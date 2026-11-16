"""A stand-in for the 1Password CLI, for the agent-auth-sync tests.

It keeps a vault as JSON in $FAKE_OP_STORE ({"vault/title": item}) and
answers the commands agent-auth-sync runs, with op's error texts. Each call's
arguments go to $FAKE_OP_STORE.calls, one JSON list per line, so a test can
check that no secret reached a command line. $FAKE_OP_FAIL makes every call
fail as op does when it cannot reach 1Password, and $FAKE_OP_HANG makes each
call sleep that many seconds first, as op does when the network drops its
packets; with $FAKE_OP_HANG_REF set, only `op read` of that reference
sleeps. While $FAKE_OP_STORE.down exists, every call fails as with
$FAKE_OP_FAIL. $FAKE_OP_AFTER_READ and $FAKE_OP_AFTER_EDIT are shell commands run
once, after the first `op read` or `op item edit` that succeeds: what another
process or machine does at that moment.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

STORE = Path(os.environ["FAKE_OP_STORE"])


def load() -> dict:
    return json.loads(STORE.read_text()) if STORE.exists() else {}


def save(items: dict) -> None:
    STORE.write_text(json.dumps(items))


def fail(message: str) -> NoReturn:
    print(f"[ERROR] 2026/11/15 12:00:00 {message}", file=sys.stderr)
    sys.exit(1)


def after(name: str) -> None:
    command = os.environ.get(f"FAKE_OP_AFTER_{name}")
    done = Path(f"{STORE}.after-{name.lower()}")
    if command and not done.exists():
        done.touch()
        subprocess.run(command, shell=True, check=True)


def flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def main(args: list[str]) -> None:
    with open(f"{STORE}.calls", "a") as calls:
        calls.write(json.dumps(args) + "\n")
    if hang := os.environ.get("FAKE_OP_HANG"):
        only = os.environ.get("FAKE_OP_HANG_REF")
        if not only or args == ["read", only]:
            time.sleep(float(hang))
    if os.environ.get("FAKE_OP_FAIL") or Path(f"{STORE}.down").exists():
        fail("could not connect to 1Password: network unreachable")
    items = load()
    match args:
        case ["whoami"]:
            print("User Type: SERVICE_ACCOUNT")
        case ["read", reference]:
            vault, title, field = reference.removeprefix("op://").split("/")
            item = items.get(f"{vault}/{title}")
            if item is None:
                fail(
                    f"could not read secret '{reference}': could not get item "
                    f'{vault}/{title}: "{title}" isn\'t an item in the "{vault}" vault.'
                )
            for entry in item["fields"]:
                if field in (entry.get("label"), entry.get("id")):
                    print(entry.get("value", ""), flush=True)
                    after("READ")
                    return
            fail(
                f"could not read secret '{reference}': item '{vault}/{title}' "
                f"does not have a field '{field}'"
            )
        case ["item", "list", *rest]:
            vault = flag(rest, "--vault")
            print(
                json.dumps(
                    [
                        {"id": item.get("id", key), "title": key.split("/", 1)[1]}
                        for key, item in items.items()
                        if key.split("/", 1)[0] == vault
                    ]
                )
            )
        case ["item", "get", title, *rest]:
            vault = flag(rest, "--vault")
            item = items.get(f"{vault}/{title}")
            if item is None:
                fail(f'"{title}" isn\'t an item in the "{vault}" vault.')
            print(json.dumps(item))
        case ["item", "edit", title, *rest]:
            vault = flag(rest, "--vault")
            if f"{vault}/{title}" not in items:
                fail(f'"{title}" isn\'t an item in the "{vault}" vault.')
            item = json.loads(Path(flag(rest, "--template")).read_text())
            item["version"] = item.get("version", 1) + 1
            items[f"{vault}/{title}"] = item
            save(items)
            after("EDIT")
        case ["item", "create", *rest]:
            vault = flag(rest, "--vault")
            item = json.loads(Path(flag(rest, "--template")).read_text())
            item["version"] = 1
            items[f"{vault}/{item['title']}"] = item
            save(items)
        case _:
            fail(f"fake op does not know {args}")


if __name__ == "__main__":
    main(sys.argv[1:])
