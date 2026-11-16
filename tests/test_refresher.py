"""The login refresher (refresher.py), against a fake `op` and a local token server.

Nothing here reaches 1Password or a real token endpoint: `op` is
tests/fake_op.py and every token URL points at a server in this process. The
tokens are made up; none may reach a log line, the summary, or a command line.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentix import auth, refresher
from agentix.errors import NotFound, OperationFailed

FAKE_OP = Path(__file__).with_name("fake_op.py")
NOW = 1_800_000_000.0  # 2027-01-15
DAY = 24 * 60 * 60


def jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )

    return f"{part({'alg': 'none'})}.{part(claims)}.c2ln"


def openai_jwt(name: str, issued: float, expires: float) -> str:
    return jwt(
        {
            "name": name,
            "iat": int(issued),
            "exp": int(expires),
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-1",
                "chatgpt_user_id": "user-1",
                "chatgpt_plan_type": "pro",
            },
        }
    )


def codex_login(name: str, refresh: str, issued: float, expires: float) -> dict:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": openai_jwt(f"id-{name}", issued, expires),
            "access_token": openai_jwt(name, issued, expires),
            "refresh_token": refresh,
            "account_id": "acct-1",
        },
        "last_refresh": refresher._iso(issued),
    }


class TokenServer:
    """Answers token requests by path; records each one."""

    def __init__(self):
        self.requests: list[dict] = []
        self.answers: dict[str, tuple[int, dict]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                kind = self.headers["Content-Type"]
                if kind == "application/json":
                    fields = json.loads(body)
                else:
                    fields = dict(urllib.parse.parse_qsl(body))
                outer.requests.append(
                    {"path": self.path, "type": kind, "fields": fields}
                )
                status, answer = outer.answers.get(self.path, (404, {"error": "nope"}))
                if callable(answer):
                    answer = answer(fields)
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RefresherTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault.json"
        op = self.tmp / "op"
        op.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_OP} "$@"\n')
        op.chmod(0o755)
        environment = patch.dict(os.environ, {"FAKE_OP_STORE": str(self.vault)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in refresher.SETTINGS:
            os.environ.pop(name, None)
        self.store = refresher.OpStore(op=str(op))
        # Every field written, as ITEM/FIELD, in order, by any OpStore.
        self.writes: list[str] = []
        real_write = refresher.OpStore.write
        test = self

        def recording_write(store, ref, value):
            real_write(store, ref, value)
            test.writes.append(ref.removeprefix("op://agentix/"))

        recorder = patch.object(refresher.OpStore, "write", recording_write)
        recorder.start()
        self.addCleanup(recorder.stop)
        self.server = TokenServer()
        self.addCleanup(self.server.close)
        self.urls = {
            "codex": self.server.url("/codex"),
            "openai": self.server.url("/openai"),
            "openai-codex": self.server.url("/openai-codex"),
            "anthropic": self.server.url("/anthropic"),
        }
        self.lines: list[str] = []
        # Every token a test plants or the server hands out; checked in tearDown.
        self.secrets: set[str] = set()

    def secret(self, value: str) -> str:
        self.secrets.add(value)
        return value

    def put(self, title: str, value) -> None:
        items = json.loads(self.vault.read_text()) if self.vault.exists() else {}
        items[f"agentix/{title}"] = {
            "id": f"id-{title}",
            "title": title,
            "category": "API_CREDENTIAL",
            "fields": [
                {"id": "notesPlain", "label": "notesPlain", "value": "keep me"},
                {
                    "id": "x1",
                    "label": "auth",
                    "type": "CONCEALED",
                    "value": json.dumps(value),
                },
            ],
        }
        self.vault.write_text(json.dumps(items))

    def get(self, title: str):
        items = json.loads(self.vault.read_text()) if self.vault.exists() else {}
        item = items.get(f"agentix/{title}")
        if item is None:
            return None
        values = [f["value"] for f in item["fields"] if f.get("label") == "auth"]
        return json.loads(values[0]) if values else None

    def calls(self) -> list[list[str]]:
        path = Path(f"{self.vault}.calls")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def written(self) -> list[str]:
        """The fields written, as ITEM/FIELD, in order, without owner fields."""
        return [ref for ref in self.writes if not ref.endswith("/owner")]

    def set_field(self, title: str, label: str, text: str) -> None:
        items = json.loads(self.vault.read_text()) if self.vault.exists() else {}
        item = items.setdefault(
            f"agentix/{title}", {"id": f"id-{title}", "title": title, "fields": []}
        )
        item["fields"] = [f for f in item["fields"] if f.get("label") != label]
        item["fields"].append({"id": label, "label": label, "value": text})
        self.vault.write_text(json.dumps(items))

    def get_field(self, title: str, label: str):
        items = json.loads(self.vault.read_text()) if self.vault.exists() else {}
        item = items.get(f"agentix/{title}") or {"fields": []}
        values = [f["value"] for f in item["fields"] if f.get("label") == label]
        return json.loads(values[0]) if values else None

    def run_once(
        self, now: float = NOW, force: bool = False, store=None, **options
    ) -> dict:
        run = refresher.Run(
            store or self.store,
            now=now,
            force=force,
            token_urls=self.urls,
            log=self.lines.append,
            write_pause=0,
            **options,
        )
        summary = run()
        self.summaries = getattr(self, "summaries", []) + [summary]
        return summary

    def tearDown(self):
        seen = "\n".join(self.lines) + json.dumps(getattr(self, "summaries", []))
        argv = "\n".join(" ".join(call) for call in self.calls())
        for secret in self.secrets:
            self.assertNotIn(secret, seen)
            self.assertNotIn(secret, argv)


class CodexTest(RefresherTestCase):
    def answer_codex(self, name: str, refresh: str):
        access = self.secret(openai_jwt(name, NOW, NOW + 10 * DAY))
        self.secret(refresh)
        self.server.answers["/codex"] = (
            200,
            {
                "access_token": access,
                "refresh_token": refresh,
                "id_token": openai_jwt("id", NOW, NOW + 10 * DAY),
            },
        )
        return access

    def test_first_run_takes_the_real_login_over_and_publishes_it_stripped(self):
        login = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        self.put("codex", login)

        summary = self.run_once()

        self.assertTrue(summary["codex"]["adopted"])
        self.assertFalse(summary["codex"]["refreshed"])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.get("codex-refresher"), login)
        public = self.get("codex")
        self.assertEqual(public["tokens"]["refresh_token"], "")
        self.assertEqual(
            public["tokens"]["access_token"], login["tokens"]["access_token"]
        )

    def test_a_due_login_is_refreshed_and_rotated_before_the_copy_is_written(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.put("codex", refresher.codex_strip(old))
        access = self.answer_codex("a2", "refresh-a2")

        summary = self.run_once()

        self.assertTrue(summary["codex"]["refreshed"], summary)
        (request,) = self.server.requests
        self.assertEqual(request["type"], "application/json")
        self.assertEqual(
            request["fields"],
            {
                "grant_type": "refresh_token",
                "client_id": refresher.CODEX_CLIENT_ID,
                "refresh_token": "refresh-a1",
                "refresh_reason": "scheduled",
            },
        )
        real = self.get("codex-refresher")
        self.assertEqual(real["tokens"]["refresh_token"], "refresh-a2")
        self.assertEqual(real["tokens"]["access_token"], access)
        self.assertEqual(real["last_refresh"], "2027-01-15T08:00:00.000000Z")
        public = self.get("codex")
        self.assertEqual(public["tokens"]["refresh_token"], "")
        self.assertEqual(public["tokens"]["access_token"], access)
        # A check that the store takes writes, the new login, then the copy.
        self.assertEqual(
            self.written(),
            ["codex-refresher/auth", "codex-refresher/auth", "codex/auth"],
        )

    def test_a_login_with_time_left_is_not_refreshed(self):
        login = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        self.put("codex-refresher", login)
        self.put("codex", refresher.codex_strip(login))

        self.run_once()
        self.run_once(now=NOW + 60)

        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.written(), [])  # nothing changed, nothing written
        self.assertIn("codex-refresher/owner", self.writes)

    def test_an_old_last_refresh_is_refreshed_even_with_time_left(self):
        login = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 8 * DAY, NOW + 30 * DAY
        )
        self.put("codex-refresher", login)
        self.answer_codex("a2", "refresh-a2")

        self.assertTrue(self.run_once()["codex"]["refreshed"])

    def test_a_refused_refresh_token_changes_nothing_and_says_so(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.put("codex", refresher.codex_strip(old))
        self.server.answers["/codex"] = (
            400,
            {
                "error": "refresh_token_reused",
                "error_description": "refresh-a1 was used",
            },
        )

        summary = self.run_once()

        self.assertFalse(summary["ok"])
        self.assertTrue(summary["codex"]["permanent"])
        self.assertIn("400 (refresh_token_reused)", summary["codex"]["error"])
        self.assertEqual(self.get("codex-refresher"), old)
        self.assertEqual(self.get("codex"), refresher.codex_strip(old))
        self.assertEqual(self.written(), ["codex-refresher/auth"])  # the check only

    def test_a_newer_real_login_pushed_by_a_machine_is_taken_over(self):
        old = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        newer = codex_login("b1", self.secret("refresh-b1"), NOW, NOW + 10 * DAY)
        self.put("codex-refresher", old)
        self.put("codex", newer)  # say, `codex login` on a machine that still pushes

        summary = self.run_once()

        self.assertTrue(summary["codex"]["adopted"])
        self.assertEqual(self.get("codex-refresher"), newer)
        self.assertEqual(self.get("codex"), refresher.codex_strip(newer))

    def test_an_older_real_login_in_the_public_field_is_replaced_not_taken(self):
        current = codex_login("b1", self.secret("refresh-b1"), NOW, NOW + 10 * DAY)
        stale = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        self.put("codex-refresher", current)
        self.put("codex", stale)

        self.run_once()

        self.assertEqual(self.get("codex-refresher"), current)
        self.assertEqual(self.get("codex"), refresher.codex_strip(current))

    def test_a_newer_stripped_copy_is_never_replaced_by_an_older_one(self):
        older = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        newer = codex_login("b1", self.secret("refresh-b1"), NOW, NOW + 10 * DAY)
        self.put("codex-refresher", older)  # say, a second refresher, or a restore
        self.put("codex", refresher.codex_strip(newer))

        self.run_once()

        self.assertEqual(self.get("codex"), refresher.codex_strip(newer))

    def test_a_new_token_that_cannot_be_stored_goes_to_the_intake_field(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.put("codex", refresher.codex_strip(old))
        self.answer_codex("a2", "refresh-a2")
        store_ref = refresher.setting("AGENTIX_REFRESHER_CODEX_STORE")

        class Flaky(refresher.OpStore):
            writes = 0

            def write(inner, ref, value):
                if ref == store_ref:
                    inner.writes += 1
                    if inner.writes > 1:  # the check passes, the save does not
                        raise OSError("disk full")  # not even a StoreError
                super().write(ref, value)

        flaky = Flaky(op=self.store.op)
        summary = self.run_once(store=flaky)

        self.assertFalse(summary["ok"])
        self.assertIn("codex-refresher/intake", summary["codex"]["error"])
        self.assertEqual(flaky.writes, 4)  # the check and three tries
        self.assertEqual(
            self.get_field("codex-refresher", "intake")["tokens"]["refresh_token"],
            "refresh-a2",
        )
        self.assertEqual(self.get("codex")["tokens"]["refresh_token"], "")
        # The next run, with 1Password writable again, takes it back and strips it.
        summary = self.run_once(now=NOW + 1800)
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(
            self.get("codex-refresher")["tokens"]["refresh_token"], "refresh-a2"
        )
        self.assertEqual(self.get("codex")["tokens"]["refresh_token"], "")
        self.assertEqual(len(self.server.requests), 1)

    def test_a_public_field_that_changes_during_the_run_is_left_for_the_next(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.put("codex", refresher.codex_strip(old))
        self.answer_codex("a2", "refresh-a2")
        public_ref = refresher.setting("AGENTIX_REFRESHER_CODEX_PUBLIC")
        other = codex_login("c1", "", NOW, NOW + 10 * DAY)
        test = self

        class Racing(refresher.OpStore):
            reads = 0

            def read(inner, ref):
                if ref == public_ref:
                    inner.reads += 1
                    if inner.reads == 2:
                        test.put("codex", other)
                return super().read(ref)

        self.run_once(store=Racing(op=self.store.op))

        self.assertEqual(self.get("codex"), other)
        self.assertEqual(
            self.get("codex-refresher")["tokens"]["refresh_token"], "refresh-a2"
        )

    def test_with_store_and_intake_unwritable_the_public_field_gets_it(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.put("codex", refresher.codex_strip(old))
        self.answer_codex("a2", "refresh-a2")

        class Flaky(refresher.OpStore):
            writes = 0

            def write(inner, ref, value):
                if "codex-refresher" in ref and not ref.endswith("/owner"):
                    inner.writes += 1
                    if inner.writes > 1:
                        raise refresher.StoreError(f"{ref}: writing 1Password failed")
                super().write(ref, value)

        summary = self.run_once(store=Flaky(op=self.store.op))

        self.assertIn("it is in op://agentix/codex/auth", summary["codex"]["error"])
        self.assertEqual(self.get("codex")["tokens"]["refresh_token"], "refresh-a2")
        # The next run takes it back from there and strips it again.
        self.assertTrue(self.run_once(now=NOW + 1800)["ok"])
        self.assertEqual(
            self.get("codex-refresher")["tokens"]["refresh_token"], "refresh-a2"
        )
        self.assertEqual(self.get("codex")["tokens"]["refresh_token"], "")

    def test_the_rescue_keeps_trying_until_the_deadline(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.answer_codex("a2", "refresh-a2")
        clock = [0.0]
        attempts = []

        class Down(refresher.OpStore):
            checked = False

            def write(inner, ref, value):
                if ref.endswith("/owner"):
                    return super().write(ref, value)
                if not inner.checked:
                    inner.checked = True
                    return super().write(ref, value)
                attempts.append(ref)
                clock[0] += 50  # each failed write takes a while
                raise refresher.StoreError(f"{ref}: writing 1Password failed")

        down = Down(op=self.store.op, timeout=10)
        summary = self.run_once(store=down, clock=lambda: clock[0], budget=600)

        self.assertIn("could not be saved anywhere", summary["codex"]["error"])
        self.assertIn("CRITICAL the new login could not be saved anywhere", self.lines)
        rescues = [ref for ref in attempts if not ref.endswith("codex-refresher/auth")]
        self.assertGreater(len(rescues), 2)  # more than one round
        self.assertLessEqual(clock[0], 600 + 100)

    def test_a_store_that_cannot_be_written_spends_no_refresh_token(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.answer_codex("a2", "refresh-a2")

        class ReadOnly(refresher.OpStore):
            def write(inner, ref, value):
                if ref.endswith("/auth"):
                    raise refresher.StoreError(f"{ref}: writing 1Password failed")
                super().write(ref, value)

        summary = self.run_once(store=ReadOnly(op=self.store.op))

        self.assertFalse(summary["ok"])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.get("codex-refresher"), old)

    def test_too_little_time_left_spends_no_refresh_token(self):
        old = codex_login(
            "a1", self.secret("refresh-a1"), NOW - 9.5 * DAY, NOW + 0.5 * DAY
        )
        self.put("codex-refresher", old)
        self.answer_codex("a2", "refresh-a2")
        run = refresher.Run(
            self.store,
            now=NOW,
            token_urls=self.urls,
            log=self.lines.append,
            write_pause=0,
        )
        self.assertGreater(run.reserve, 2 * 60)

        summary = self.run_once(budget=run.reserve - 1)

        self.assertFalse(summary["codex"]["refreshed"])
        self.assertEqual(self.server.requests, [])
        self.assertIn(
            "codex: too little time left in this run to refresh; the next run does",
            self.lines,
        )

    def test_the_run_fits_the_functions_timeout(self):
        run = refresher.Run(self.store, now=NOW)
        self.assertLess(refresher.RUN_BUDGET, refresher.FUNCTION_TIMEOUT)
        # The last refresh may start with only the reserve left; all of its
        # saves and the rescue then still end before Modal stops the function.
        self.assertLess(refresher.RUN_BUDGET + 60, refresher.FUNCTION_TIMEOUT)
        self.assertLess(run.reserve, refresher.RUN_BUDGET / 2)

    def test_a_login_handed_in_through_the_intake_is_taken(self):
        old = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        newer = codex_login("b1", self.secret("refresh-b1"), NOW, NOW + 10 * DAY)
        self.put("codex-refresher", old)
        self.put("codex", refresher.codex_strip(old))
        self.set_field("codex-refresher", "intake", json.dumps(newer))

        summary = self.run_once()

        self.assertTrue(summary["codex"]["adopted"])
        self.assertEqual(self.get("codex-refresher"), newer)
        self.assertEqual(self.get("codex"), refresher.codex_strip(newer))
        # An older one handed in later is ignored.
        self.set_field("codex-refresher", "intake", json.dumps(old))
        self.assertFalse(self.run_once(now=NOW + 60)["codex"]["adopted"])

    def test_order_comes_from_the_token_not_last_refresh(self):
        current = codex_login("a1", self.secret("refresh-a1"), NOW, NOW + 10 * DAY)
        fast_clock = codex_login(
            "b1", self.secret("refresh-b1"), NOW - DAY, NOW + 9 * DAY
        )
        fast_clock["last_refresh"] = refresher._iso(NOW + 5 * DAY)  # clock ahead
        self.put("codex-refresher", current)
        self.put("codex", fast_clock)

        self.assertFalse(self.run_once()["codex"]["adopted"])
        self.assertEqual(self.get("codex-refresher"), current)

    def test_a_store_owned_by_another_app_is_refused(self):
        login = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        self.put("codex-refresher", login)
        self.set_field("codex-refresher", "owner", "other-refresher")

        summary = self.run_once()

        self.assertIn(
            "belongs to the refresher app 'other-refresher'", summary["codex"]["error"]
        )
        self.assertEqual(self.written(), [])

    def test_a_missing_login_says_how_to_seed_it(self):
        summary = self.run_once()

        self.assertIn("agentix auth refresher seed", summary["codex"]["error"])

    def test_force_refreshes_a_login_that_is_not_due(self):
        login = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        self.put("codex-refresher", login)
        self.answer_codex("a2", "refresh-a2")

        self.assertTrue(self.run_once(force=True)["codex"]["refreshed"])


class PiCase(RefresherTestCase):
    def logins(self, expires: float) -> dict:
        return {
            "openai": {
                "type": "oauth",
                "access": self.secret("pi-openai-access-1"),
                "refresh": self.secret("pi-openai-refresh-1"),
                "expires": int(expires * 1000),
                "clientId": "client-xyz",
                "scopes": ["openid", refresher.OPENAI_DIRECT_SCOPE],
            },
            "openai-codex": {
                "type": "oauth",
                "access": self.secret(openai_jwt("pi-codex-1", NOW - 9 * DAY, expires)),
                "refresh": self.secret("pi-codex-refresh-1"),
                "expires": int(expires * 1000),
                "accountId": "acct-1",
            },
            "anthropic": {
                "type": "oauth",
                "access": self.secret("pi-ant-access-1"),
                "refresh": self.secret("pi-ant-refresh-1"),
                "expires": int(expires * 1000),
            },
        }

    def answer_all(self):
        self.server.answers["/openai"] = (
            200,
            {
                "access_token": self.secret("pi-openai-access-2"),
                "refresh_token": self.secret("pi-openai-refresh-2"),
                "expires_in": 3600,
                "scope": f"openid {refresher.OPENAI_DIRECT_SCOPE}",
                "id_token": "x.y.z",
            },
        )
        self.server.answers["/openai-codex"] = (
            200,
            {
                "access_token": self.secret(
                    openai_jwt("pi-codex-2", NOW, NOW + 10 * DAY)
                ),
                "refresh_token": self.secret("pi-codex-refresh-2"),
                "expires_in": 10 * DAY,
            },
        )
        self.server.answers["/anthropic"] = (
            200,
            {
                "access_token": self.secret("pi-ant-access-2"),
                "refresh_token": self.secret("pi-ant-refresh-2"),
                "expires_in": 28800,
            },
        )


class PiTest(PiCase):
    def test_each_provider_is_refreshed_as_pi_does_it(self):
        logins = self.logins(NOW + 600)
        self.put("pi-refresher", logins)
        self.put("pi", {"zai": {"type": "api_key", "key": "k"}})
        self.answer_all()

        summary = self.run_once()

        self.assertEqual(summary["pi"]["errors"], [], summary)
        requests = {r["path"]: r for r in self.server.requests}
        self.assertEqual(
            requests["/openai"],
            {
                "path": "/openai",
                "type": "application/x-www-form-urlencoded",
                "fields": {
                    "grant_type": "refresh_token",
                    "client_id": "client-xyz",
                    "refresh_token": "pi-openai-refresh-1",
                    "resource": "https://api.openai.com/v1",
                },
            },
        )
        self.assertEqual(
            requests["/openai-codex"]["fields"],
            {
                "grant_type": "refresh_token",
                "refresh_token": "pi-codex-refresh-1",
                "client_id": refresher.CODEX_CLIENT_ID,
            },
        )
        self.assertEqual(
            requests["/openai-codex"]["type"], "application/x-www-form-urlencoded"
        )
        self.assertEqual(requests["/anthropic"]["type"], "application/json")
        self.assertEqual(
            requests["/anthropic"]["fields"],
            {
                "grant_type": "refresh_token",
                "client_id": refresher.ANTHROPIC_CLIENT_ID,
                "refresh_token": "pi-ant-refresh-1",
            },
        )
        real = self.get("pi-refresher")
        self.assertEqual(
            real["openai"],
            {
                "type": "oauth",
                "access": "pi-openai-access-2",
                "refresh": "pi-openai-refresh-2",
                "expires": int((NOW + 3600 - 180) * 1000),
                "clientId": "client-xyz",
                "scopes": ["openid", refresher.OPENAI_DIRECT_SCOPE],
            },
        )
        self.assertEqual(real["openai-codex"]["refresh"], "pi-codex-refresh-2")
        self.assertEqual(real["openai-codex"]["accountId"], "acct-1")
        self.assertEqual(real["openai-codex"]["expires"], int((NOW + 10 * DAY) * 1000))
        self.assertEqual(real["anthropic"]["expires"], int((NOW + 28800 - 300) * 1000))
        public = self.get("pi")
        self.assertEqual(public["zai"], {"type": "api_key", "key": "k"})
        for name in ("openai", "openai-codex", "anthropic"):
            self.assertEqual(public[name], {**real[name], "refresh": ""})
        self.assertEqual(self.written()[-1], "pi/auth")

    def test_one_failing_provider_leaves_the_others_refreshed(self):
        logins = self.logins(NOW + 600)
        self.put("pi-refresher", logins)
        self.put("pi", {name: refresher.pi_strip(e) for name, e in logins.items()})
        self.answer_all()
        self.server.answers["/anthropic"] = (503, {"error": "overloaded"})

        summary = self.run_once()

        self.assertFalse(summary["ok"])
        self.assertIn("503", summary["pi"]["providers"]["anthropic"]["error"])
        self.assertFalse(summary["pi"]["providers"]["anthropic"]["permanent"])
        real = self.get("pi-refresher")
        self.assertEqual(real["openai"]["refresh"], "pi-openai-refresh-2")
        self.assertEqual(real["anthropic"], logins["anthropic"])
        public = self.get("pi")
        self.assertEqual(public["openai"]["access"], "pi-openai-access-2")
        self.assertEqual(public["anthropic"], refresher.pi_strip(logins["anthropic"]))

    def test_pi_providers_are_due_by_their_own_expiry(self):
        logins = self.logins(NOW + 10 * DAY)
        logins["anthropic"]["expires"] = int((NOW + 3600) * 1000)  # under 2 hours left
        self.put("pi-refresher", logins)
        self.answer_all()

        summary = self.run_once()

        self.assertEqual([r["path"] for r in self.server.requests], ["/anthropic"])
        self.assertTrue(summary["pi"]["providers"]["anthropic"]["refreshed"])
        self.assertFalse(summary["pi"]["providers"]["openai"]["refreshed"])

    def test_an_unsupported_provider_stays_in_the_store_only(self):
        self.put(
            "pi",
            {
                "github-copilot": {
                    "type": "oauth",
                    "access": self.secret("gh-access"),
                    "refresh": self.secret("gh-refresh"),
                    "expires": int((NOW + DAY) * 1000),
                },
                "anthropic": self.logins(NOW + DAY)["anthropic"],
            },
        )

        summary = self.run_once()

        self.assertIn(
            "not supported", summary["pi"]["providers"]["github-copilot"]["error"]
        )
        self.assertEqual(
            self.get("pi-refresher")["github-copilot"]["refresh"], "gh-refresh"
        )
        public = self.get("pi")
        self.assertNotIn("github-copilot", public)
        self.assertEqual(public["anthropic"]["refresh"], "")

    def test_a_newer_entry_in_the_public_field_is_taken_per_provider(self):
        logins = self.logins(NOW + DAY)
        self.put("pi-refresher", logins)
        newer = {
            "type": "oauth",
            "access": self.secret("pi-ant-access-9"),
            "refresh": self.secret("pi-ant-refresh-9"),
            "expires": int((NOW + 2 * DAY) * 1000),
        }
        self.put(
            "pi",
            {
                **{n: refresher.pi_strip(e) for n, e in logins.items()},
                "anthropic": newer,
            },
        )

        summary = self.run_once()

        self.assertTrue(summary["pi"]["providers"]["anthropic"]["adopted"])
        self.assertEqual(self.get("pi-refresher")["anthropic"], newer)
        self.assertEqual(self.get("pi")["anthropic"], refresher.pi_strip(newer))
        self.assertEqual(self.get("pi-refresher")["openai"], logins["openai"])


class PiSafetyTest(PiCase):
    def test_a_pi_login_that_cannot_be_stored_goes_to_the_intake_field(self):
        logins = self.logins(NOW + 600)
        self.put("pi-refresher", logins)
        self.answer_all()
        store_ref = refresher.setting("AGENTIX_REFRESHER_PI_STORE")

        class Flaky(refresher.OpStore):
            writes = 0

            def write(inner, ref, value):
                if ref == store_ref:
                    inner.writes += 1
                    if inner.writes > 1:
                        raise ValueError("bad JSON from op")
                super().write(ref, value)

        summary = self.run_once(store=Flaky(op=self.store.op))

        self.assertIn("pi-refresher/intake", summary["pi"]["error"])
        handed = self.get_field("pi-refresher", "intake")
        self.assertEqual(handed["anthropic"]["refresh"], "pi-ant-refresh-2")
        # The next run takes it back.
        self.run_once(now=NOW + 60)
        self.assertEqual(
            self.get("pi-refresher")["anthropic"]["refresh"], "pi-ant-refresh-2"
        )

    def test_the_summary_names_providers_left_out_of_the_public_field(self):
        self.put(
            "pi-refresher",
            {
                "xai": {
                    "type": "oauth",
                    "access": "a",
                    "refresh": self.secret("xai-r"),
                    "expires": 5,
                }
            },
        )
        summary = self.run_once()
        self.assertEqual(summary["pi"]["left_out"], ["xai"])

    def test_a_handed_in_pi_entry_is_taken_per_provider(self):
        logins = self.logins(NOW + DAY)
        self.put("pi-refresher", logins)
        newer = {
            **logins["anthropic"],
            "refresh": self.secret("pi-ant-refresh-7"),
            "expires": int((NOW + 2 * DAY) * 1000),
        }
        self.set_field(
            "pi-refresher",
            "intake",
            json.dumps({"anthropic": newer, "openai": logins["openai"]}),
        )

        summary = self.run_once()

        self.assertTrue(summary["pi"]["providers"]["anthropic"]["adopted"])
        self.assertFalse(summary["pi"]["providers"]["openai"]["adopted"])
        self.assertEqual(self.get("pi-refresher")["anthropic"], newer)


class WindowTest(unittest.TestCase):
    def test_half_the_lifetime_within_bounds(self):
        # 10-day token: refreshed in its last day.
        self.assertFalse(refresher.due(NOW + 1.1 * DAY, NOW - 8.9 * DAY, NOW))
        self.assertTrue(refresher.due(NOW + 0.9 * DAY, NOW - 9.1 * DAY, NOW))
        # 8-hour token: in its last 4 hours.
        self.assertFalse(refresher.due(NOW + 4.1 * 3600, NOW - 3.9 * 3600, NOW))
        self.assertTrue(refresher.due(NOW + 3.9 * 3600, NOW - 4.1 * 3600, NOW))
        # 1-hour token: never later than an hour ahead, so every run.
        self.assertTrue(refresher.due(NOW + 3000, NOW - 600, NOW))

    def test_without_an_expiry_every_two_hours(self):
        self.assertFalse(refresher.due(None, None, NOW, NOW - 3600))
        self.assertTrue(refresher.due(None, None, NOW, NOW - 2 * 3600))
        self.assertTrue(refresher.due(None, None, NOW))
        login = codex_login("a", "r", NOW, NOW + DAY)
        login["tokens"]["access_token"] = "not-a-jwt"
        login["last_refresh"] = refresher._iso(NOW - 3600)
        self.assertFalse(refresher.codex_due(login, NOW))
        login["last_refresh"] = refresher._iso(NOW - 3 * 3600)
        self.assertTrue(refresher.codex_due(login, NOW))

    def test_without_an_issue_time_two_hours_ahead(self):
        self.assertFalse(refresher.due(NOW + 2.1 * 3600, None, NOW))
        self.assertTrue(refresher.due(NOW + 1.9 * 3600, None, NOW))
        self.assertTrue(refresher.due(None, None, NOW))


class StrippedShapeTest(unittest.TestCase):
    """The copies keep every key with an empty refresh token: Codex rejects a
    file without tokens.refresh_token, and pi's read-only store an OAuth
    entry whose refresh is not a string (see docs/auth-refresher.md)."""

    def test_codex(self):
        login = codex_login("a", "refresh-a", NOW, NOW + DAY)
        stripped = refresher.codex_strip(login)
        self.assertEqual(stripped["tokens"]["refresh_token"], "")
        self.assertEqual(set(stripped["tokens"]), set(login["tokens"]))
        self.assertEqual(login["tokens"]["refresh_token"], "refresh-a")
        self.assertFalse(refresher.codex_real(stripped))

    def test_pi(self):
        entry = {
            "type": "oauth",
            "access": "a",
            "refresh": "r",
            "expires": 1,
            "accountId": "x",
        }
        stripped = refresher.pi_strip(entry)
        self.assertEqual(stripped, {**entry, "refresh": ""})
        self.assertIsInstance(stripped["refresh"], str)


@unittest.skipUnless(
    shutil.which("codex") and shutil.which("pi"),
    "needs the codex and pi binaries on PATH",
)
class AgentsAcceptTheCopiesTest(unittest.TestCase):
    """Codex and pi themselves read the stripped copies, offline."""

    def test_codex_and_pi_accept_the_copies(self):
        with tempfile.TemporaryDirectory() as home:
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.lower().endswith("_proxy")
            }
            now = time.time()
            codex_home = Path(home, "codex")
            codex_home.mkdir()
            login = codex_login("a", "refresh-a", now, now + DAY)
            (codex_home / "auth.json").write_text(
                json.dumps(refresher.codex_strip(login))
            )
            result = subprocess.run(
                ["codex", "login", "status"],
                env={**env, "CODEX_HOME": str(codex_home)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertIn("Logged in using ChatGPT", result.stdout + result.stderr)

            pi_dir = Path(home, "pi")
            pi_dir.mkdir()
            entry = {
                "type": "oauth",
                "access": "fake-access",
                "refresh": "refresh-a",
                "expires": int((now + DAY) * 1000),
            }
            (pi_dir / "auth.json").write_text(
                json.dumps({"anthropic": refresher.pi_strip(entry)})
            )
            result = subprocess.run(
                ["pi", "auth", "check", "--provider", "anthropic", "--json"],
                env={**env, "PI_CODING_AGENT_DIR": str(pi_dir), "PI_OFFLINE": "1"},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                json.loads(result.stdout)["status"], "ready", result.stderr
            )


class SeedTest(RefresherTestCase):
    def test_seed_hands_logins_in_and_the_next_run_takes_them(self):
        login = codex_login("a1", self.secret("refresh-a1"), NOW, NOW + DAY)
        codex_file = self.tmp / "codex.json"
        codex_file.write_text(json.dumps(login))
        pi_file = self.tmp / "pi.json"
        entry = {
            "type": "oauth",
            "access": "x",
            "refresh": self.secret("pi-seed-refresh"),
            "expires": int((NOW + DAY) * 1000),
        }
        pi_file.write_text(
            json.dumps({"anthropic": entry, "zai": {"type": "api_key", "key": "k"}})
        )
        openai = {
            "type": "oauth",
            "access": "o",
            "refresh": "o",
            "expires": int((NOW + DAY) * 1000),
            "clientId": "c",
        }
        self.put("pi-refresher", {"openai": openai})

        done = refresher.seed(self.store, str(codex_file), str(pi_file))

        self.assertEqual(done["pi"]["providers"], ["anthropic"])
        # Only the intake fields are written; the run takes them, serialized.
        self.assertEqual(
            self.written(), ["codex-refresher/intake", "pi-refresher/intake"]
        )
        self.run_once()
        self.assertEqual(self.get("codex-refresher"), login)
        self.assertEqual(set(self.get("pi-refresher")), {"openai", "anthropic"})

    def test_seed_refuses_a_login_older_than_the_store(self):
        old = codex_login("a1", self.secret("refresh-a1"), NOW - DAY, NOW + 9 * DAY)
        newer = codex_login("a2", self.secret("refresh-a2"), NOW, NOW + 10 * DAY)
        self.put("codex-refresher", newer)
        codex_file = self.tmp / "codex.json"
        codex_file.write_text(json.dumps(old))

        with self.assertRaisesRegex(refresher.StoreError, "spent refresh token"):
            refresher.seed(self.store, str(codex_file))
        self.assertEqual(self.written(), [])

        # --force hands it in, and the next run takes it although it is older.
        refresher.seed(self.store, str(codex_file), force=True)
        self.assertTrue(self.run_once()["codex"]["adopted"])
        self.assertEqual(self.get("codex-refresher"), old)
        # Taken once: the intake no longer forces it.
        self.put("codex-refresher", newer)
        self.assertFalse(self.run_once(now=NOW + 60)["codex"]["adopted"])
        self.assertEqual(self.get("codex-refresher"), newer)

    def test_seed_refuses_older_pi_providers(self):
        entry = {
            "type": "oauth",
            "access": "x",
            "refresh": self.secret("pi-r1"),
            "expires": 5,
        }
        newer = {**entry, "refresh": self.secret("pi-r2"), "expires": 9}
        self.put("pi-refresher", {"anthropic": newer})
        pi_file = self.tmp / "pi.json"
        pi_file.write_text(json.dumps({"anthropic": entry}))

        with self.assertRaisesRegex(refresher.StoreError, "anthropic login is newer"):
            refresher.seed(self.store, "", str(pi_file))

    def test_seeding_the_same_login_again_changes_nothing(self):
        login = codex_login("a1", self.secret("refresh-a1"), NOW, NOW + DAY)
        self.put("codex-refresher", login)
        codex_file = self.tmp / "codex.json"
        codex_file.write_text(json.dumps(login))

        done = refresher.seed(self.store, str(codex_file))

        self.assertEqual(done["codex"], "already the refresher's login")
        self.assertEqual(self.written(), [])

    def test_seed_refuses_a_stripped_file(self):
        stripped = refresher.codex_strip(codex_login("a1", "r", NOW, NOW + DAY))
        codex_file = self.tmp / "codex.json"
        codex_file.write_text(json.dumps(stripped))
        with patch.object(refresher, "OpStore", lambda: self.store):
            with self.assertRaisesRegex(
                OperationFailed, "no Codex login with a refresh token"
            ):
                auth.refresher_seed(str(codex_file), "")
        self.assertIsNone(self.get("codex-refresher"))


class OpStoreTest(RefresherTestCase):
    def test_write_keeps_the_items_other_fields_and_creates_missing_items(self):
        self.put("codex", {"a": 1})
        self.store.write("op://agentix/codex/auth", '{"b":2}')
        self.store.write("op://agentix/fresh/auth", '{"c":3}')

        items = json.loads(self.vault.read_text())
        labels = [f["label"] for f in items["agentix/codex"]["fields"]]
        self.assertEqual(labels, ["notesPlain", "auth"])
        self.assertEqual(self.get("codex"), {"b": 2})
        self.assertEqual(self.get("fresh"), {"c": 3})
        self.assertIsNone(self.store.read("op://agentix/missing/auth"))
        # No template outlives its call.
        self.assertEqual(
            list(Path(tempfile.gettempdir()).glob("agentix-refresher.*")), []
        )

    def test_failures_name_the_field_not_the_value(self):
        os.environ["FAKE_OP_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_OP_FAIL")
        with self.assertRaisesRegex(
            refresher.StoreError, "op://agentix/codex/auth: reading"
        ):
            self.store.read("op://agentix/codex/auth")
        with self.assertRaisesRegex(refresher.StoreError, "network unreachable"):
            self.store.write("op://agentix/codex/auth", "secret-value")


class CommandsTest(unittest.TestCase):
    """`agentix auth refresher ...` against a stand-in for Modal."""

    def test_run_now_without_a_deployment_says_how_to_deploy(self):
        import modal

        function = unittest.mock.Mock()
        function.hydrate.side_effect = modal.exception.NotFoundError("no app")
        with patch.object(modal.Function, "from_name", return_value=function):
            with self.assertRaisesRegex(NotFound, "agentix auth refresher deploy"):
                auth.refresher_run()

    def test_run_now_fails_when_the_run_did_not_finish(self):
        import modal

        function = unittest.mock.Mock()
        function.remote.return_value = {"ok": False, "codex": {"error": "x"}}
        with (
            patch.object(modal.Function, "from_name", return_value=function),
            patch("sys.stdout"),
        ):
            with self.assertRaises(OperationFailed):
                auth.refresher_run(force=True)
        function.remote.assert_called_once_with(force=True)

    def test_status_of_an_app_that_never_ran(self):
        import modal

        function = unittest.mock.Mock()
        missing = unittest.mock.Mock()
        missing.get.side_effect = modal.exception.NotFoundError("no dict")
        with (
            patch.object(modal.Function, "from_name", return_value=function),
            patch.object(modal.Dict, "from_name", return_value=missing),
        ):
            status = auth.refresher_status()
        self.assertTrue(status["deployed"])
        self.assertEqual(status["last_run"], "never")
        self.assertIn("op://agentix/codex-refresher/auth", status["store"])


class ModalAppTest(unittest.TestCase):
    def test_the_function_runs_on_a_schedule_with_the_op_secret(self):
        self.assertEqual(refresher.SCHEDULE, "17,47 * * * *")
        self.assertEqual(refresher.SECRET_NAME, "1password")
        # The image pins the profile's op, by version and hash.
        self.assertIn(refresher.OP_VERSION, refresher.OP_URL)
        self.assertEqual(len(refresher.OP_SHA256), 64)
        secrets = repr(refresher.refresh._spec_.secrets)
        self.assertIn("1password", secrets)
        self.assertIn("AGENTIX_REFRESHER_CODEX_STORE", secrets)


if __name__ == "__main__":
    unittest.main()
