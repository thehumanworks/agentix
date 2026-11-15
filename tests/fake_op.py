"""A stand-in for the 1Password CLI, for the agent-auth-sync tests.

It keeps a vault as JSON in $FAKE_OP_STORE ({"vault/title": item}) and
answers the commands agent-auth-sync runs, with op's error texts. Each call's
arguments go to $FAKE_OP_STORE.calls, one JSON list per line, so a test can
check that no secret reached a command line. $FAKE_OP_FAIL makes every call
fail as op does when it cannot reach 1Password.
"""

import json
import os
import sys
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


def flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def main(args: list[str]) -> None:
    with open(f"{STORE}.calls", "a") as calls:
        calls.write(json.dumps(args) + "\n")
    if os.environ.get("FAKE_OP_FAIL"):
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
                    print(entry.get("value", ""))
                    return
            fail(
                f"could not read secret '{reference}': item '{vault}/{title}' "
                f"does not have a field '{field}'"
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
