"""`sandbox sync`: copy uncommitted changes between this checkout and a sandbox.

The work is done by agentix-sync, one script that runs on both sides: the
image installs it from nix/, and this host runs the same file from the
repository. This module only carries its input and output across and turns
its report into a result or an error. See the script for how changes move
and what it refuses.

Keep this module free of heavy imports: the command line reads it on every run.
"""

import asyncio
import contextlib
import re
import shlex
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import InvalidRequest, NotFound, OperationFailed
from .layout import NIX_DIR, WORKDIR

SCRIPT = NIX_DIR / "home-manager" / "modules" / "scripts" / "agentix-sync.sh"
PROTOCOL = "2"
# What a side runs agentix-sync with, unless the sandbox lacks it: the image's
# command, or this repository's copy.
INSTALLED = ("agentix-sync",)

# A bundle is held in memory on its way. Larger ones are refused, and from
# the warning size on the result says so: usually untracked files that are
# not ignored, such as build output.
MAX_BUNDLE = 1 << 30
WARN_BUNDLE = 100 << 20

# Runs a command on one side: its arguments and input; its exit code, output
# and errors.
Runner = Callable[[Sequence[str], bytes], Awaitable[tuple[int, bytes, bytes]]]


@dataclass
class Side:
    label: str
    run: Runner
    path: str
    command: Sequence[str] = INSTALLED

    async def call(
        self, action: str, stdin: bytes = b"", *args: str
    ) -> tuple[int, str, bytes]:
        code, out, err = await self.run(
            [*self.command, action, self.path, *args], stdin
        )
        return code, err.decode(errors="replace").strip(), out


def script_command() -> list[str]:
    """agentix-sync from this repository, for a side that does not install it."""
    return ["bash", "-c", SCRIPT.read_text(), "agentix-sync"]


async def run_here(args: Sequence[str], stdin: bytes) -> tuple[int, bytes, bytes]:
    """A Runner for this host."""

    def run() -> tuple[int, bytes, bytes]:
        try:
            done = subprocess.run(list(args), input=stdin, capture_output=True)
        except FileNotFoundError as error:
            # What a shell reports for a command it cannot find.
            return 127, b"", str(error).encode()
        return done.returncode, done.stdout, done.stderr

    return await asyncio.to_thread(run)


def checkout_name(info: dict[str, Any]) -> str:
    """The directory `sandbox up --repo` clones this checkout's repository into."""
    match = _github(info["origin"])
    return match.rsplit("/", 1)[-1] if match else Path(info["toplevel"]).name


def default_path(info: dict[str, Any]) -> str:
    return f"{WORKDIR}/{checkout_name(info)}"


async def info(
    side: Side, fallback: Sequence[str] | None = None, hint: str = ""
) -> dict[str, Any]:
    """What the checkout of `side` is. With `fallback`, a side that lacks the
    command, or has another protocol, runs that command instead. `hint` follows
    the error for a missing checkout."""
    code, err, out = await side.call("info")
    stale = code == 0 and _protocol(out) != PROTOCOL
    if fallback is not None and (code == 127 or stale):
        side.command = fallback
        code, err, out = await side.call("info")
    if code in (4, 5):
        detail = err.removeprefix("agentix-sync: ")
        raise NotFound(f"{detail} in {side.label}.{hint}")
    if code != 0:
        raise OperationFailed(f"agentix-sync info failed in {side.label}: {err}")
    result: dict[str, Any] = {"root": [], "known": []}
    for line in out.decode().splitlines():
        key, _, value = line.partition(" ")
        if key in ("root", "known"):
            result[key].append(value)
        elif key == "synced":
            result[key] = tuple(value.split(" "))
        else:
            result[key] = value
    return result


def check_same_repository(here: dict[str, Any], there: dict[str, Any]) -> None:
    """Refuse two checkouts that are not of one repository."""
    mine, theirs = _github(here["origin"]), _github(there["origin"])
    if mine and theirs and mine.lower() != theirs.lower():
        raise InvalidRequest(
            f"{there['toplevel']} in the sandbox is a checkout of {theirs}, but "
            f"{here['toplevel']} is of {mine}. Pass --path=DIR to name the "
            "sandbox's checkout of this repository."
        )
    if not set(here["root"]) & set(there["root"]):
        raise InvalidRequest(
            f"{there['toplevel']} in the sandbox and {here['toplevel']} share "
            "no history, so they are not one repository. Pass --path=DIR to "
            "name the sandbox's checkout of this repository."
        )


async def sync(here: Side, there: Side, pull: bool = False) -> dict[str, Any]:
    """Copy the uncommitted changes of `here` to `there`, or the other way with
    `pull`. Both checkouts must be of one repository. Without a path, `there`
    is the checkout that `sandbox up --repo` would have made."""
    here_info = await info(here)
    if not there.path:
        there.path = default_path(here_info)
    there_info = await info(
        there,
        fallback=script_command(),
        hint=" Start a sandbox with its clone with sandbox up --repo=OWNER/NAME, "
        "or pass --path=DIR to name the checkout.",
    )
    check_same_repository(here_info, there_info)
    sender, receiver = (there, here) if pull else (here, there)
    sender_info, receiver_info = (
        (there_info, here_info) if pull else (here_info, there_info)
    )
    known = "".join(f"{sha}\n" for sha in receiver_info["known"]).encode()
    code, err, bundle = await sender.call("pack", known)
    if code != 0:
        raise OperationFailed(f"agentix-sync pack failed in {sender.label}: {err}")
    if len(bundle) > MAX_BUNDLE:
        raise OperationFailed(
            f"The changes in {sender.label} come to {_mib(len(bundle))}, more than "
            f"{_mib(MAX_BUNDLE)}, so nothing was sent. Untracked files that are "
            "not ignored are sent too: add large ones to .gitignore."
        )
    code, err, out = await receiver.call(
        "apply", bundle, *_base(here_info, there_info, sender_info)
    )
    report = _report(out.decode(errors="replace"))
    where = f"{receiver.label} ({receiver.path})"
    undo = _undo_hint(receiver, here, report)
    if code == 3:
        raise OperationFailed(_conflict_message(sender, receiver, report))
    if code != 0 or "result" not in report:
        message = f"agentix-sync apply failed in {where}: {err}"
        if undo:
            # It failed after saving the working tree: say how to get it back.
            message += f"\nThe working tree from before is saved: {undo}."
        raise OperationFailed(message)
    # Now both sides hold the sender's snapshot. If this fails, the next sync
    # merges from the sender's HEAD instead.
    with contextlib.suppress(Exception):
        await sender.call("mark", b"", report["incoming"])
    result: dict[str, Any] = {
        "from": f"{sender.label} ({sender.path})",
        "to": where,
        "result": report["result"],
        "changed": report["changed"],
    }
    if undo:
        result["backup"] = report["backup"][0].split(" ", 1)[0]
        result["undo"] = undo
    if len(bundle) > WARN_BUNDLE:
        result["warning"] = (
            f"{_mib(len(bundle))} was sent. Untracked files that are not ignored "
            "are sent each time and kept in the backups: add large ones to "
            ".gitignore."
        )
    if report["sender-head"] != report["head"]:
        result["note"] = (
            f"{receiver.label} is on commit {report['head'][:12]} and "
            f"{sender.label} on {report['sender-head'][:12]}: only the uncommitted "
            f"changes were merged in. {sender.label}'s commit is in "
            "refs/agentix-sync/incoming^ there."
        )
    return result


def _undo_hint(receiver: Side, here: Side, report: dict[str, Any]) -> str:
    """How to put back the working tree that apply saved, or "" if it saved
    none."""
    if not report["backup"]:
        return ""
    ref, _, commit = report["backup"][0].partition(" ")
    if receiver is here:
        command = shlex.join(["bash", str(SCRIPT), "undo", receiver.path])
    elif tuple(receiver.command) == INSTALLED:
        command = shlex.join(["agentix-sync", "undo", receiver.path])
    else:
        # The image lacks agentix-sync; this repository's copy can run there.
        command = (
            f"agentix-sync undo {shlex.quote(receiver.path)} with "
            f"{SCRIPT.relative_to(SCRIPT.parents[4])} from this repository"
        )
    return (
        f"run {command} in {receiver.label}; the backup is {ref} (commit {commit[:12]})"
    )


def _mib(size: int) -> str:
    return f"{size / (1 << 20):.0f} MiB"


def _base(here: dict[str, Any], there: dict[str, Any], sender: dict[str, Any]):
    """The last snapshot both sides applied, if the sender is still on its
    commit, as the base to merge from: then a file that one side changed
    since is no conflict. Otherwise none, and the sender's HEAD is the base."""
    synced = here.get("synced")
    if synced and synced == there.get("synced") and synced[1] == sender["head"]:
        return [synced[0]]
    return []


def _conflict_message(sender: Side, receiver: Side, report: dict[str, Any]) -> str:
    lines = [
        f"Nothing changed: {receiver.label} ({receiver.path}) has changes that "
        f"conflict with those in {sender.label}."
    ]
    if report["conflict"]:
        lines.append("Changed on both sides in ways that do not merge:")
        lines += [f"  {path}" for path in report["conflict"]]
    if report["ignored"]:
        lines.append(
            f"Added in {sender.label}, but in {receiver.label} a file Git "
            "ignores is there:"
        )
        lines += [f"  {path}" for path in report["ignored"]]
    lines.append(
        "Commit, move aside, or undo those changes on one side, then sync again."
    )
    return "\n".join(lines)


def _report(text: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "changed": [],
        "conflict": [],
        "ignored": [],
        "backup": [],
    }
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in ("changed", "conflict", "ignored", "backup"):
            report[key].append(value.replace("\t", " "))
        elif key:
            report[key] = value
    return report


def _protocol(out: bytes) -> str:
    first = out.split(b"\n", 1)[0].decode(errors="replace")
    return first.removeprefix("protocol ") if first.startswith("protocol ") else ""


_GITHUB = re.compile(
    r"(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/]+@)github\.com[:/]"
    r"(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?"
)


def _github(url: str) -> str:
    """OWNER/NAME of a GitHub remote URL, or "" for any other URL."""
    match = _GITHUB.fullmatch(url.strip())
    return match["repo"] if match else ""
