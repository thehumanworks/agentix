"""agent-auth-sync, run as a real shell script against a fake `op`.

The fake keeps its vault in a JSON file (see fake_op.py), so each test sets
up both sides, runs the script, and reads what it left on each.
"""

import contextlib
import json
import os
import plistlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentix import auth

SCRIPT = auth.SCRIPT
FAKE_OP = Path(__file__).with_name("fake_op.py")

# Values a test plants; none may reach a command line or the output.
SECRETS = ("access-old", "access-new", "refresh-old", "refresh-new", "pi-a", "pi-b")


def codex_auth(token: str, refreshed: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": token, "refresh_token": f"refresh-{token[7:]}"},
        "last_refresh": refreshed,
    }


class AuthSyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.codex = self.home / ".codex" / "auth.json"
        self.pi = self.home / ".pi" / "agent" / "auth.json"
        self.store = self.tmp / "vault.json"
        op = self.tmp / "op"
        op.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_OP} "$@"\n')
        op.chmod(0o755)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "FAKE_OP_STORE": str(self.store),
            "AGENT_AUTH_SYNC_OP": str(op),
            "AGENT_AUTH_SYNC_INTERVAL": "1",
            # Generous, so that a busy test machine still sees the first sync.
            "AGENT_AUTH_SYNC_ENSURE_WAIT": "15",
            "AGENT_AUTH_SYNC_FILES": (
                f"op://agentix/codex/auth {self.codex}\n"
                "# a comment\n"
                f"op://agentix/pi/auth ~/.pi/agent/auth.json\n"
            ),
        }

    # --- the two sides ---

    def put_field(self, title: str, value, field: str = "auth") -> None:
        items = json.loads(self.store.read_text()) if self.store.exists() else {}
        text = value if isinstance(value, str) else json.dumps(value)
        items[f"agentix/{title}"] = {
            "id": f"id-{title}",
            "title": title,
            "category": "API_CREDENTIAL",
            "fields": [
                {"id": "notesPlain", "label": "notesPlain", "value": ""},
                {"id": "username", "label": "username", "type": "STRING"},
                {"id": "expires", "label": "expires", "type": "DATE"},
                {
                    "id": "validFrom",
                    "label": "valid from",
                    "type": "DATE",
                    "value": "1",
                },
                {"id": "x1", "label": field, "type": "CONCEALED", "value": text},
            ],
        }
        self.store.write_text(json.dumps(items))

    def field(self, title: str, label: str = "auth"):
        items = json.loads(self.store.read_text())
        item = items.get(f"agentix/{title}")
        if item is None:
            return None
        values = [f["value"] for f in item["fields"] if f.get("label") == label]
        return values[0] if values else None

    def write_file(self, path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n")

    def read_file(self, path: Path):
        return json.loads(path.read_text())

    def calls(self) -> list[list[str]]:
        calls = Path(f"{self.store}.calls")
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [os.environ.get("AUTH_SYNC_TEST_SHELL", "sh"), str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )
        for secret in SECRETS:
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def sync(self, **env: str) -> subprocess.CompletedProcess:
        result = self.run_script("sync", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def tearDown(self):
        for call in self.calls():
            for secret in SECRETS:
                self.assertNotIn(secret, " ".join(call))


class FirstSyncTest(AuthSyncTestCase):
    def test_pulls_into_a_machine_without_the_files(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        self.put_field(
            "pi", {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )

        self.sync()

        self.assertEqual(
            self.read_file(self.codex)["tokens"]["access_token"], "access-old"
        )
        self.assertEqual(self.read_file(self.pi)["anthropic"]["access"], "pi-a")
        for path in (self.codex, self.pi):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_pushes_as_one_line_into_a_new_field_and_a_new_item(self):
        self.put_field("codex", "", field="other")  # the item, without the field
        self.write_file(self.codex, codex_auth("access-old", "2026-11-01T00:00:00Z"))
        self.write_file(
            self.pi, {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )

        self.sync()

        pushed = self.field("codex")
        self.assertNotIn("\n", pushed)
        self.assertEqual(json.loads(pushed), self.read_file(self.codex))
        self.assertEqual(json.loads(self.field("pi"))["anthropic"]["access"], "pi-a")

    def test_formatting_alone_is_no_change(self):
        value = codex_auth("access-old", "2026-11-01T00:00:00Z")
        self.put_field("codex", value)
        self.write_file(self.codex, value)  # pretty-printed; the field is one line
        self.put_field("pi", {"openai": {"type": "api_key", "key": "pi-a"}})
        self.write_file(self.pi, {"openai": {"key": "pi-a", "type": "api_key"}})
        before = self.codex.read_text()

        self.sync()
        self.sync()

        self.assertEqual(self.codex.read_text(), before)
        self.assertFalse([c for c in self.calls() if c[:2] == ["item", "edit"]])

    def test_an_empty_pi_file_is_not_pushed(self):
        self.put_field(
            "pi", {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )
        self.write_file(self.pi, {})  # what pi writes before anyone signs in

        self.sync()

        self.assertEqual(self.read_file(self.pi)["anthropic"]["access"], "pi-a")

    def test_empty_on_both_sides_writes_nothing(self):
        self.sync()

        self.assertFalse(self.codex.exists())
        self.assertFalse(self.store.exists())

    def test_pi_taking_its_lock_during_a_pull_stops_it(self):
        self.put_field(
            "pi", {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )
        self.pi.parent.mkdir(parents=True)
        lock = Path(f"{self.pi}.lock")

        self.sync(FAKE_OP_AFTER_READ=f"mkdir {lock}")

        self.assertFalse(self.pi.exists())
        lock.rmdir()
        self.sync()
        self.assertEqual(self.read_file(self.pi)["anthropic"]["access"], "pi-a")


class LaterSyncTest(AuthSyncTestCase):
    def setUp(self):
        super().setUp()
        self.old = codex_auth("access-old", "2026-11-01T00:00:00.000001Z")
        self.new = codex_auth("access-new", "2026-11-09T00:00:00.000001Z")
        self.put_field("codex", self.old)
        self.sync()  # both sides now agree on self.old

    def test_a_local_refresh_is_pushed(self):
        self.write_file(self.codex, self.new)

        self.sync()

        self.assertEqual(json.loads(self.field("codex")), self.new)

    def test_a_push_keeps_the_items_other_fields(self):
        self.write_file(self.codex, self.new)

        self.sync()

        item = json.loads(self.store.read_text())["agentix/codex"]
        labels = [f["label"] for f in item["fields"]]
        # An empty date would come back as the epoch, so it is left out.
        self.assertEqual(labels, ["notesPlain", "username", "valid from", "auth"])

    def test_a_refresh_elsewhere_is_pulled(self):
        self.put_field("codex", self.new)

        self.sync()

        self.assertEqual(self.read_file(self.codex), self.new)

    def test_a_deleted_file_comes_back(self):
        self.codex.unlink()

        self.sync()

        self.assertEqual(self.read_file(self.codex), self.old)

    def test_both_changed_keeps_the_later_codex_refresh(self):
        self.put_field("codex", self.new)
        self.write_file(self.codex, codex_auth("access-old", "2026-11-02T00:00:00Z"))

        self.sync()

        self.assertEqual(self.read_file(self.codex), self.new)
        self.assertEqual(json.loads(self.field("codex")), self.new)

    def test_both_changed_merges_pi_providers(self):
        self.put_field(
            "pi",
            {
                "anthropic": {"type": "oauth", "access": "pi-a", "expires": 10},
                "openai": {"type": "oauth", "access": "pi-a", "expires": 50},
            },
        )
        self.sync()
        self.write_file(
            self.pi,
            {
                "anthropic": {"type": "oauth", "access": "pi-b", "expires": 20},
                "openai": {"type": "oauth", "access": "pi-a", "expires": 50},
                "xai": {"type": "api_key", "key": "pi-b"},
            },
        )
        self.put_field(
            "pi",
            {
                "anthropic": {"type": "oauth", "access": "pi-a", "expires": 10},
                "openai": {"type": "oauth", "access": "pi-b", "expires": 60},
            },
        )

        result = self.sync()

        merged = {
            "anthropic": {"type": "oauth", "access": "pi-b", "expires": 20},
            "openai": {"type": "oauth", "access": "pi-b", "expires": 60},
            "xai": {"type": "api_key", "key": "pi-b"},
        }
        self.assertEqual(self.read_file(self.pi), merged)
        self.assertEqual(json.loads(self.field("pi")), merged)
        self.assertIn("both changed", result.stderr)

    def test_a_refresh_during_a_pull_is_kept_and_pushed_next(self):
        self.put_field("codex", self.new)
        refreshed = codex_auth("access-new", "2026-11-10T00:00:00Z")
        later = self.tmp / "refreshed.json"
        later.write_text(json.dumps(refreshed))
        # Codex refreshes after the script read the file, before it renames.
        hook = f"cp {later} {self.codex}"

        self.sync(FAKE_OP_AFTER_READ=hook)

        self.assertEqual(self.read_file(self.codex), refreshed)
        self.sync()
        self.assertEqual(json.loads(self.field("codex")), refreshed)

    MERGED = {
        "anthropic": {"type": "oauth", "access": "pi-b", "expires": 20},
        "openai": {"type": "oauth", "access": "pi-b", "expires": 60},
    }

    def crossing_push(self) -> Path:
        """Both sides agree; then pi refreshes anthropic here, and the returned
        store is the vault after another machine pushed its openai refresh."""
        first = {
            "anthropic": {"type": "oauth", "access": "pi-a", "expires": 10},
            "openai": {"type": "oauth", "access": "pi-a", "expires": 50},
        }
        self.put_field("pi", first)
        self.sync()
        agreed = self.store.read_text()
        self.put_field("pi", {**first, "openai": self.MERGED["openai"]})
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text(self.store.read_text())
        self.store.write_text(agreed)
        self.write_file(self.pi, {**first, "anthropic": self.MERGED["anthropic"]})
        return elsewhere

    def test_a_push_at_the_same_time_elsewhere_is_merged(self):
        elsewhere = self.crossing_push()

        result = self.sync(FAKE_OP_AFTER_EDIT=f"cp {elsewhere} {self.store}")

        self.assertEqual(self.read_file(self.pi), self.MERGED)
        self.assertEqual(json.loads(self.field("pi")), self.MERGED)
        self.assertIn("another machine pushed at the same time", result.stderr)

    def test_a_push_that_cannot_be_read_back_is_merged_next_time(self):
        elsewhere = self.crossing_push()
        down = Path(f"{self.store}.down")

        # 1Password goes away just after the push and the other machine's.
        self.run_script(
            "sync", FAKE_OP_AFTER_EDIT=f"cp {elsewhere} {self.store} && touch {down}"
        )
        down.unlink()
        self.sync()

        self.assertEqual(self.read_file(self.pi), self.MERGED)
        self.assertEqual(json.loads(self.field("pi")), self.MERGED)

    def test_a_merge_left_for_pis_lock_is_merged_next_time(self):
        elsewhere = self.crossing_push()
        lock = Path(f"{self.pi}.lock")

        # pi takes its lock just as the push is read back.
        self.sync(FAKE_OP_AFTER_EDIT=f"cp {elsewhere} {self.store} && mkdir {lock}")
        lock.rmdir()
        self.sync()

        self.assertEqual(self.read_file(self.pi), self.MERGED)
        self.assertEqual(json.loads(self.field("pi")), self.MERGED)

    def test_a_pi_provider_removed_locally_is_removed_remotely(self):
        both = {
            "anthropic": {"type": "oauth", "access": "pi-a", "expires": 10},
            "openai": {"type": "api_key", "key": "pi-a"},
        }
        self.put_field("pi", both)
        self.sync()
        self.write_file(self.pi, {"openai": both["openai"]})  # /logout anthropic

        self.sync()

        self.assertEqual(json.loads(self.field("pi")), {"openai": both["openai"]})


class FailureTest(AuthSyncTestCase):
    def test_op_failure_leaves_the_file_alone_and_fails(self):
        self.write_file(self.codex, codex_auth("access-old", "2026-11-01T00:00:00Z"))
        before = self.codex.read_text()

        result = self.run_script("sync", FAKE_OP_FAIL="1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("reading 1Password failed: could not connect", result.stderr)
        self.assertEqual(self.codex.read_text(), before)

    def test_a_hanging_op_is_given_up(self):
        self.write_file(self.codex, codex_auth("access-old", "2026-11-01T00:00:00Z"))

        started = time.monotonic()
        result = self.run_script(
            "sync", FAKE_OP_HANG="20", AGENT_AUTH_SYNC_OP_TIMEOUT="1"
        )

        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result.returncode, 1)
        self.assertIn("op did not answer within 1 seconds", result.stderr)

    def test_a_file_that_is_not_json_is_left_alone(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text('{"half": ')

        result = self.run_script("sync")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.codex.read_text(), '{"half": ')

    def test_pi_holding_its_lock_is_waited_out(self):
        self.put_field(
            "pi", {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )
        self.pi.parent.mkdir(parents=True)
        Path(f"{self.pi}.lock").mkdir()

        self.sync()

        self.assertFalse(self.pi.exists())

    def test_a_lock_left_behind_is_ignored(self):
        self.put_field(
            "pi", {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )
        self.pi.parent.mkdir(parents=True)
        lock = Path(f"{self.pi}.lock")
        lock.mkdir()
        os.utime(lock, (time.time() - 120, time.time() - 120))

        self.sync()

        self.assertEqual(self.read_file(self.pi)["anthropic"]["access"], "pi-a")
        self.assertFalse(lock.exists())

    def test_a_file_pi_is_writing_is_read_again(self):
        self.put_field(
            "pi", {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )
        self.pi.parent.mkdir(parents=True)
        self.pi.write_text('{"anthropic": ')  # half written
        # pi takes its lock just after the script looked for it.
        lock = Path(f"{self.pi}.lock")
        jq = self.tmp / "jq"
        jq.write_text(
            f'#!/bin/sh\n[ -e {lock} ] || mkdir {lock}\nexec {shutil.which("jq")} "$@"\n'
        )
        jq.chmod(0o755)

        result = self.run_script("sync", AGENT_AUTH_SYNC_JQ=str(jq))

        self.assertNotIn("not JSON", result.stderr)
        state = self.home / ".local" / "state" / "agent-auth-sync"
        self.assertFalse((state / "op___agentix_pi_auth.seen").exists())

    def test_a_bad_reference_is_reported(self):
        result = self.run_script(
            "sync", AGENT_AUTH_SYNC_FILES="op://agentix/codex /tmp/x"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("not an op://VAULT/ITEM/FIELD reference", result.stderr)


class CommandsTest(AuthSyncTestCase):
    def test_status_names_each_state_without_values(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        self.write_file(
            self.pi, {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )

        result = self.run_script("status")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("watcher: not running", result.stdout)
        self.assertIn("only in 1Password; sync pulls it", result.stdout)
        self.assertIn("only in the file; sync pushes it", result.stdout)

    def test_ensure_starts_one_watcher_that_pushes_changes(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))

        token = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_fake"}
        first = self.run_script("ensure", **token)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            self.read_file(self.codex)["tokens"]["access_token"], "access-old"
        )
        pid = self.wait_for_watcher()
        self.assertEqual(self.run_script("ensure", **token).returncode, 0)
        self.assertEqual(self.wait_for_watcher(), pid)

        new = codex_auth("access-new", "2026-11-09T00:00:00Z")
        self.write_file(self.codex, new)
        deadline = time.monotonic() + 20
        while json.loads(self.field("codex")) != new:
            self.assertLess(time.monotonic(), deadline, "the watcher did not push")
            time.sleep(0.2)

        self.stop(pid)

    def test_ensure_returns_soon_when_op_hangs(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))

        started = time.monotonic()
        result = self.run_script(
            "ensure",
            OP_SERVICE_ACCOUNT_TOKEN="ops_fake",
            FAKE_OP_HANG="20",
            AGENT_AUTH_SYNC_ENSURE_WAIT="1",
        )

        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still running after 1 seconds", result.stderr)
        self.stop(self.wait_for_watcher())
        # Nothing of it is left waiting on op, which it stopped too.
        deadline = time.monotonic() + 5
        while self.watchers():
            self.assertLess(time.monotonic(), deadline, "a process was left")
            time.sleep(0.2)

    def test_ensure_waits_only_for_the_agents_own_file(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        self.put_field(
            "pi", {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )
        slow_pi = {
            "OP_SERVICE_ACCOUNT_TOKEN": "ops_fake",
            "FAKE_OP_HANG": "20",
            "FAKE_OP_HANG_REF": "op://agentix/pi/auth",
            "AGENT_AUTH_SYNC_ENSURE_WAIT": "6",
        }

        started = time.monotonic()
        result = self.run_script("ensure", str(self.codex), **slow_pi)

        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(
            self.read_file(self.codex)["tokens"]["access_token"], "access-old"
        )

        # pi's own wrapper waits for pi's file, which is still on its way.
        result = self.run_script("ensure", str(self.pi), **slow_pi)
        self.assertIn("still running after 6 seconds", result.stderr)
        self.stop(self.wait_for_watcher())

    def test_two_ensures_at_once_leave_one_watcher(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        env = {**self.env, "OP_SERVICE_ACCOUNT_TOKEN": "ops_fake"}
        command = [os.environ.get("AUTH_SYNC_TEST_SHELL", "sh"), str(SCRIPT), "ensure"]
        starts = [
            subprocess.Popen(
                command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            for _ in range(2)
        ]
        for start in starts:
            start.communicate(timeout=30)
            self.assertEqual(start.returncode, 0)
        pid = self.wait_for_watcher()

        deadline = time.monotonic() + 10
        while (watchers := self.watchers()) != [pid]:
            self.assertLess(time.monotonic(), deadline, f"watchers: {watchers}")
            time.sleep(0.2)
        self.stop(pid)

    def watchers(self) -> list[int]:
        """The watchers of this test's home, by PID."""
        found = []
        for proc in Path("/proc").iterdir():
            # Another user's process is never one of ours, and a user other
            # than root may not even probe it.
            if not proc.name.isdigit() or not _ours(proc):
                continue
            if not _alive(int(proc.name)):
                continue
            try:
                args = (proc / "cmdline").read_bytes().split(b"\0")
                environ = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if (
                str(SCRIPT).encode() in args
                and b"watch" in args
                and f"HOME={self.home}".encode() in environ
            ):
                found.append(int(proc.name))
        return found

    def test_a_change_made_under_pis_lock_is_pushed_after_it(self):
        old = {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        new = {"anthropic": {"type": "oauth", "access": "pi-b", "expires": 9}}
        self.put_field("pi", old)
        self.run_script("ensure", OP_SERVICE_ACCOUNT_TOKEN="ops_fake")
        pid = self.wait_for_watcher()
        self.assertEqual(self.read_file(self.pi), old)

        lock = Path(f"{self.pi}.lock")
        lock.mkdir()
        self.write_file(self.pi, new)
        time.sleep(2.5)  # two looks at the file, each left alone
        self.assertEqual(json.loads(self.field("pi")), old)
        lock.rmdir()

        # Within the watcher's look at the files, not its 2-minute remote read.
        deadline = time.monotonic() + 5
        while json.loads(self.field("pi")) != new:
            self.assertLess(time.monotonic(), deadline, "the change was not pushed")
            time.sleep(0.2)
        self.stop(pid)

    def test_a_failed_sync_leaves_no_plaintext_behind(self):
        self.write_file(self.codex, codex_auth("access-old", "2026-11-01T00:00:00Z"))
        self.put_field("codex", "not json")
        self.write_file(
            self.pi, {"anthropic": {"type": "oauth", "access": "pi-a", "expires": 5}}
        )
        self.put_field("pi", '{"half": ')

        result = self.run_script("ensure", OP_SERVICE_ACCOUNT_TOKEN="ops_fake")
        pid = self.wait_for_watcher()

        self.assertEqual(result.returncode, 1)
        scratch = list(self.tmp.glob("agent-auth-sync.*"))
        self.assertEqual(len(scratch), 1)  # the watcher's, while it runs
        for leftover in scratch[0].rglob("*"):
            text = leftover.read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, text, leftover.name)
        self.stop(pid)

    def test_a_watcher_whose_state_is_removed_ends(self):
        self.run_script("ensure", OP_SERVICE_ACCOUNT_TOKEN="ops_fake")
        pid = self.wait_for_watcher()

        shutil.rmtree(self.home / ".local" / "state" / "agent-auth-sync")

        self.wait_until_gone(pid)

    def test_a_dead_watchers_lock_is_taken_over(self):
        state = self.home / ".local" / "state" / "agent-auth-sync"
        (state / "watch.lock").mkdir(parents=True)
        ended = subprocess.Popen(["true"])
        ended.wait()
        (state / "watch.lock" / "pid").write_text(f"{ended.pid}\n")

        self.run_script("ensure", OP_SERVICE_ACCOUNT_TOKEN="ops_fake")

        deadline = time.monotonic() + 15
        while (pid := self.wait_for_watcher()) == ended.pid:
            self.assertLess(time.monotonic(), deadline, "the lock was not taken over")
            time.sleep(0.1)
        self.stop(pid)

    def stop(self, pid: int) -> None:
        self.assertIn("stopped the watcher", self.run_script("stop").stdout)
        self.wait_until_gone(pid)

    def wait_until_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 15
        while _alive(pid):
            self.assertLess(time.monotonic(), deadline, "the watcher did not stop")
            time.sleep(0.2)

    def test_ensure_without_a_token_does_nothing(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))

        result = self.run_script("ensure")

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertFalse(self.codex.exists())
        self.assertEqual(self.calls(), [])

    def test_ensure_reads_the_saved_token(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        token = self.tmp / "token"
        token.write_text("ops_fake")

        result = self.run_script("ensure", AGENT_AUTH_SYNC_TOKEN_FILE=str(token))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.codex.exists())
        self.stop(self.wait_for_watcher())

    def test_ensure_reads_the_profiles_token(self):
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        token = self.tmp / "profile-token"
        token.write_text("ops_fake")

        result = self.run_script(
            "ensure", AGENT_AUTH_SYNC_PROFILE_TOKEN_FILE=str(token)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.codex.exists())
        self.stop(self.wait_for_watcher())

    def test_watch_needs_a_token(self):
        result = self.run_script("watch")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no service account token", result.stderr)

    def wait_for_watcher(self) -> int:
        pid_file = (
            self.home / ".local" / "state" / "agent-auth-sync" / "watch.lock" / "pid"
        )
        deadline = time.monotonic() + 15
        while not pid_file.exists() or not pid_file.read_text().strip():
            self.assertLess(time.monotonic(), deadline, "no watcher started")
            time.sleep(0.1)
        pid = int(pid_file.read_text())
        self.addCleanup(_kill, pid)  # even when the test fails before stop
        return pid

    def test_save_token_needs_the_token_in_the_shell(self):
        result = self.run_script("save-token")

        self.assertEqual(result.returncode, 1)
        self.assertIn("OP_SERVICE_ACCOUNT_TOKEN is not set", result.stderr)

    def test_save_token_keeps_it_root_only(self):
        result = self.run_script("save-token", OP_SERVICE_ACCOUNT_TOKEN="ops_fake")

        self.assertEqual(result.returncode, 0, result.stderr)
        token = self.home / ".config" / "agent-auth-sync" / "op-token"
        self.assertEqual(token.read_text(), "ops_fake")
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)

    def test_version_and_usage(self):
        self.assertRegex(self.run_script("--version").stdout, r"^agent-auth-sync \d")
        self.assertEqual(self.run_script("frobnicate").returncode, 2)


class InstallTest(AuthSyncTestCase):
    """install and uninstall, with stand-ins for uname and the service managers."""

    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "service-calls"
        for name in ("systemctl", "launchctl"):
            stub = self.bin / name
            stub.write_text(f'#!/bin/sh\necho "{name} $*" >> {self.log}\n')
            stub.chmod(0o755)
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["OP_SERVICE_ACCOUNT_TOKEN"] = "ops_fake"
        self.program = self.home / ".local" / "bin" / "agent-auth-sync"
        self.config = self.home / ".config" / "agent-auth-sync"

    def system(self, name: str) -> None:
        uname = self.bin / "uname"
        uname.write_text(f"#!/bin/sh\necho {name}\n")
        uname.chmod(0o755)

    def service_calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_linux_installs_a_systemd_user_service(self):
        self.system("Linux")

        result = self.run_script("install")

        self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.home / ".config/systemd/user/agent-auth-sync.service").read_text()
        self.assertIn(f'ExecStart="{self.program}" watch', unit)
        self.assertIn("Restart=always", unit)
        self.assertIn(f'Environment="AGENT_AUTH_SYNC_CONFIG_DIR={self.config}"', unit)
        self.assertIn(
            "systemctl --user restart agent-auth-sync.service", self.service_calls()
        )
        self.assert_installed()

        result = self.run_script("uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            (self.home / ".config/systemd/user/agent-auth-sync.service").exists()
        )
        self.assertFalse(self.program.exists())
        self.assertFalse((self.config / "op-token").exists())

    def test_macos_installs_a_launchd_agent(self):
        self.system("Darwin")

        result = self.run_script("install")

        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.home / "Library/LaunchAgents/dev.agentix.agent-auth-sync.plist"
        text = plist.read_text()
        self.assertIn(f"<string>{self.program}</string>", text)
        self.assertIn("<key>KeepAlive</key>", text)
        uid = os.getuid()
        self.assertIn(f"launchctl bootstrap gui/{uid} {plist}", self.service_calls())
        self.assert_installed()
        parsed = plistlib.loads(plist.read_bytes())
        self.assertEqual(parsed["ProgramArguments"], [str(self.program), "watch"])
        self.assertEqual(
            parsed["EnvironmentVariables"],
            {"AGENT_AUTH_SYNC_CONFIG_DIR": str(self.config)},
        )

        self.assertEqual(self.run_script("uninstall").returncode, 0)
        self.assertFalse(plist.exists())

    def test_the_profiles_token_file_is_never_written_or_removed(self):
        self.system("Linux")
        profile = self.tmp / "run" / "op-token"
        profile.parent.mkdir()
        profile.write_text("ops_profile")
        self.env["AGENT_AUTH_SYNC_PROFILE_TOKEN_FILE"] = str(profile)

        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual((self.config / "op-token").read_text(), "ops_fake")
        self.assertEqual(self.run_script("uninstall").returncode, 0)
        self.assertFalse((self.config / "op-token").exists())
        self.assertEqual(profile.read_text(), "ops_profile")

        # Even when the token file is set to the profile's.
        same = {"AGENT_AUTH_SYNC_TOKEN_FILE": str(profile)}
        result = self.run_script("save-token", **same)
        self.assertEqual(result.returncode, 1)
        self.assertIn("never writes", result.stderr)
        result = self.run_script("uninstall", **same)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile.read_text(), "ops_profile")

    def test_install_needs_the_token_in_the_shell(self):
        self.system("Linux")
        del self.env["OP_SERVICE_ACCOUNT_TOKEN"]

        result = self.run_script("install")

        self.assertEqual(result.returncode, 1)
        self.assertIn("OP_SERVICE_ACCOUNT_TOKEN is not set", result.stderr)
        self.assertFalse(self.program.exists())

    def assert_installed(self):
        """The copy runs on its own, with the config that install wrote."""
        self.assertTrue(self.program.read_text().startswith("#!/bin/sh\n"))
        self.assertEqual((self.config / "op-token").read_text(), "ops_fake")
        self.put_field("codex", codex_auth("access-old", "2026-11-01T00:00:00Z"))
        env = {
            "PATH": self.env["PATH"],
            "HOME": str(self.home),
            "FAKE_OP_STORE": str(self.store),
            "AGENT_AUTH_SYNC_CONFIG_DIR": str(self.config),
        }
        result = subprocess.run(
            [str(self.program), "sync"], env=env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.codex.exists())


class CliTest(unittest.TestCase):
    def test_agentix_auth_runs_this_checkouts_script(self):
        from unittest.mock import patch

        from agentix.__main__ import main
        from agentix.errors import OperationFailed

        with patch("agentix.auth.subprocess.run") as run:
            run.return_value.returncode = 0
            main(["auth", "status"])
            run.assert_called_once_with(["/bin/sh", str(SCRIPT), "status"], check=False)

            run.return_value.returncode = 3
            with self.assertRaisesRegex(OperationFailed, "install failed"):
                auth.run("install")


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def _ours(proc: Path) -> bool:
    """Whether the process /proc/PID runs as this test's user."""
    try:
        return proc.stat().st_uid == os.getuid()
    except OSError:  # it ended
        return False


def _alive(pid: int) -> bool:
    """Whether PID runs; a zombie that no one reaped yet has ended. Another
    user's process runs too, though only root may signal it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = Path(f"/proc/{pid}/status")
    return not status.exists() or "(zombie)" not in status.read_text()


if __name__ == "__main__":
    unittest.main()
