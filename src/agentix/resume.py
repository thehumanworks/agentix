"""Continue a command that a sandbox's timeout cut off. See docs/resume.md.

`sandbox exec NAME COMMAND --resume` runs COMMAND under a wrapper that keeps
a marker file in the sandbox while it runs. The marker is in the exit
snapshot, so a sandbox restored from it knows what was cut off and how to
continue it. Nothing is resumed that was not started with --resume.

Keep the top of this module free of heavy imports: the command line reads
RULES on every `sandbox exec --resume`.
"""

import asyncio
import json
import os
import secrets
import shlex
import uuid
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple, TypedDict

from .errors import InvalidRequest, OperationFailed


class Rule(NamedTuple):
    """How one harness continues a run that was cut off. Data, not code.

    A command matches when it starts with `start`; its first word matches by
    base name and is kept as given. `first` goes after those words when the
    command starts, and `then` replaces nothing but goes there instead when
    it continues. "{session}" in either is a new UUID for each run, so the
    run continues its own conversation. The prompt is the argument after a
    word of `prompt_after` ("" means right after `start`); it continues as
    CONTINUE_PROMPT, so a cut-off task is not given again. A command whose
    prompt cannot be found, or that has a word of `refuse`, is not resumed
    by the rule. With `subcommand`, `then` is a subcommand that takes the
    prompt but not the other words, so they go before it and only the
    prompt after it.
    """

    start: tuple[str, ...]
    first: tuple[str, ...]
    then: tuple[str, ...]
    prompt_after: tuple[str, ...]
    refuse: tuple[str, ...] = ()
    subcommand: bool = False


# Each rule was checked against the harness's --help and, for codex and pi,
# by starting and continuing a run against a stand-in model server.
# claude 2.1.322: --session-id takes a UUID, and --resume without
# --fork-session keeps it, so every resume continues one session.
# pi 1.5.2: --session-id opens the workdir's session with that ID, or
# creates it, so pi does the same with one flag.
# codex 0.167.1: `codex exec` cannot be given a session ID, so it continues
# the latest session in the workdir with `codex exec resume --last`. That
# subcommand refuses most options of `codex exec` (-s, -C, --add-dir ...),
# which therefore go before it. -i takes several files and would take
# `resume` as one; --ephemeral and pi's --no-session keep no session.
RULES: dict[str, Rule] = {
    "claude": Rule(
        start=("claude",),
        first=("--session-id", "{session}"),
        then=("--resume", "{session}"),
        prompt_after=("-p", "--print"),
        refuse=("-c", "--continue", "-r", "--resume", "--session-id", "--fork-session"),
    ),
    "codex": Rule(
        start=("codex", "exec"),
        first=(),
        then=("resume", "--last"),
        prompt_after=("",),
        refuse=("resume", "fork", "review", "-i", "--image", "--ephemeral"),
        subcommand=True,
    ),
    "pi": Rule(
        start=("pi",),
        first=("--session-id", "{session}"),
        then=("--session-id", "{session}"),
        prompt_after=("-p", "--print"),
        refuse=(
            "-c",
            "--continue",
            "-r",
            "--resume",
            "--session",
            "--session-id",
            "--fork",
            "--no-session",
        ),
    ),
}
# What a resumed agent is told instead of its task.
CONTINUE_PROMPT = (
    "Your sandbox reached its time limit and was restored from a snapshot of "
    "its files. Carry on where you left off. Check what is already done "
    "before you repeat any step."
)

# In /root, so the exit snapshot has it; /run is a tmpfs.
RUNS_DIR = "/root/.local/state/agentix/runs"
# A watcher restores a name at most this many times.
MAX_RESUMES = 3
# Modal ends a sandbox at its timeout with exit code 124, and a command that
# runs in it then returns 128, a moment before the sandbox reports its code.
TIMEOUT_EXIT_CODE = 124
CUT_OFF_EXIT_CODE = 128
END_WAIT = 30
POLL_INTERVAL = 1.0
RESTORE_ATTEMPTS = 3

# $1 is RUNS_DIR, $2 the run ID, $3 the marker, then the command. The marker
# is removed when the command exits, whatever its exit code, so one that is
# left means the sandbox ended while the command ran.
WRAPPER = """set -u
mkdir -p "$1" && printf '%s\\n' "$3" > "$1/$2.tmp" && mv "$1/$2.tmp" "$1/$2.json" \
  || { echo "agentix: cannot write the resume marker in $1" >&2; exit 125; }
marker=$1/$2.json
shift 3
"$@"
code=$?
rm -f "$marker"
exit "$code"
"""
# Prints every marker in $1, one per line.
READ_MARKERS = """for file in "$1"/*.json; do
  if [ -e "$file" ]; then cat "$file"; fi
done
"""


class Run(TypedDict):
    id: str
    # The sandbox the command runs in.
    sandbox_id: str
    command: list[str]
    resume: list[str]
    started_at: str
    # How often the run was resumed before.
    resumes: int


Notify = Callable[[str], None]
Output = Callable[[Run, dict[str, Any]], None]


def plan(argv: Sequence[str], resume: object) -> tuple[list[str], list[str], str]:
    """The command to start, the command that continues it, and why.

    `resume` is True to use the rule for `argv`, or the command, as one
    string, that continues it; `argv` then starts unchanged. A command with
    no rule that fits is refused.
    """
    if isinstance(resume, str):
        words = shlex.split(resume)
        if not words:
            raise InvalidRequest("--resume='' names no command.")
        return list(argv), words, "--resume"
    if resume is not True:
        raise InvalidRequest(
            f"--resume takes no value, or a command in quotes, as in "
            f"--resume='./build.sh --continue'; not {resume!r}."
        )
    for name, rule in RULES.items():
        n = len(rule.start)
        if not (
            len(argv) >= n
            and os.path.basename(argv[0]) == rule.start[0]
            and tuple(argv[1:n]) == rule.start[1:]
        ):
            continue
        rest = list(argv[n:])
        prompt = _prompt(rest, rule.prompt_after)
        clash = [word for word in rest if word.split("=")[0] in rule.refuse]
        if prompt is None or clash:
            places = [word or " ".join(rule.start) for word in rule.prompt_after]
            why = (
                f"it already says which session to use ({clash[0]})"
                if clash
                else f"its prompt is not right after {' or '.join(places)}"
            )
            raise InvalidRequest(
                f"The {name} rule cannot resume `{shlex.join(argv)}`: {why}. Say "
                "how it continues with --resume='COMMAND'."
            )
        session = str(uuid.uuid4())
        head = [argv[0], *rule.start[1:]]
        fill = [word.replace("{session}", session) for word in rule.first]
        then = [word.replace("{session}", session) for word in rule.then]
        if rule.subcommand:
            continued = [*rest[:prompt], *rest[prompt + 1 :], *then, CONTINUE_PROMPT]
        else:
            continued = [*then, *rest[:prompt], CONTINUE_PROMPT, *rest[prompt + 1 :]]
        return [*head, *fill, *rest], [*head, *continued], f"the {name} rule"
    known = ", ".join(" ".join(rule.start) for rule in RULES.values())
    raise InvalidRequest(
        f"No resume rule for `{shlex.join(argv)}`, so it would not be resumed. "
        f"Rules: {known}. Say how it continues with --resume='COMMAND'."
    )


def _prompt(rest: list[str], after: tuple[str, ...]) -> int | None:
    """The index in `rest` of the prompt, or None."""
    places = [0] if "" in after else []
    places += [i + 1 for i, word in enumerate(rest) if word in after]
    for place in places:
        if place < len(rest) and not rest[place].startswith("-"):
            return place
    return None


async def start(
    session,
    target: str,
    argv: Sequence[str],
    resume: object,
    notify: Notify,
    output: Output,
) -> dict[str, Any]:
    """Run `argv` in the running sandbox `target`, resuming it after timeouts."""
    from . import lifecycle, store

    if not argv:
        raise InvalidRequest("Provide a command to run.")
    command, then, how = plan(argv, resume)
    sandbox_id, name = await lifecycle.running(session, target)
    if not name:
        raise InvalidRequest(
            f"{sandbox_id} has no name, so its timeout leaves no snapshot to "
            "resume from. Start one with agentix sandbox up --name=NAME."
        )
    record = await store.load(session, name)
    if not record["options"].get("exit_snapshot", True):
        raise InvalidRequest(
            f"{name} was started with --no-snapshot, so its timeout leaves no "
            "snapshot to resume from."
        )
    run: Run = {
        "id": secrets.token_hex(8),
        "sandbox_id": sandbox_id,
        "command": command,
        "resume": then,
        "started_at": store.now(),
        "resumes": 0,
    }
    notify(
        f"Running `{shlex.join(command)}` in {name} ({sandbox_id}). If {name} "
        f"reaches its timeout first, agentix restores it and continues with "
        f"`{shlex.join(then)}` ({how})."
    )
    return await _watch(session, name, sandbox_id, [run], notify, output)


async def resume(
    session, target: str, notify: Notify, output: Output
) -> dict[str, Any]:
    """Restore the stopped name `target` and continue what was cut off in it."""
    sandbox_id, snapshot, name, runs = await _restore(session, target)
    how = _ENDED.get(snapshot["kind"], "ended")
    if not runs:
        notify(
            f"Restored {name} from snapshot {snapshot['id']} as {sandbox_id}. "
            f"No command started with --resume was running when "
            f"{snapshot['sandbox_id']} {how}, so nothing was resumed."
        )
        return {"name": name, "sandbox_id": sandbox_id, "snapshot": snapshot["id"]}
    for run, was in runs:
        notify(_resumed(name, sandbox_id, snapshot, run, was))
    return await _watch(
        session, name, sandbox_id, [run for run, _ in runs], notify, output
    )


_ENDED = {
    "timeout": "reached its timeout",
    "down": "was stopped with agentix sandbox down",
    "exit": "ended",
    "manual": "was saved with agentix sandbox snapshot",
}


def _resumed(name, sandbox_id, snapshot, run: Run, was: list[str], count="") -> str:
    count = count or f"resume {run['resumes']} of this run"
    return (
        f"`{shlex.join(was)}` was running when {snapshot['sandbox_id']} "
        f"{_ENDED.get(snapshot['kind'], 'ended')}. Restored {name} from snapshot "
        f"{snapshot['id']} as {sandbox_id}; continuing with "
        f"`{shlex.join(run['command'])}` ({count})."
    )


async def _watch(
    session,
    name: str,
    sandbox_id: str,
    runs: list[Run],
    notify: Notify,
    output: Output,
) -> dict[str, Any]:
    """Run `runs` in `sandbox_id`; at its timeout, restore it and resume them."""
    restores = 0
    while True:
        codes = await asyncio.gather(
            *(_run_once(session, name, sandbox_id, run, notify, output) for run in runs)
        )
        result = {
            "name": name,
            "sandbox_id": sandbox_id,
            "resumes": restores,
            "runs": [
                {"command": run["command"], "exit_code": code}
                for run, code in zip(runs, codes)
            ],
        }
        # Commands that exited by themselves, failed or not, are done.
        if not any(code in (None, CUT_OFF_EXIT_CODE) for code in codes):
            return result
        ended = await _exit_code(session, sandbox_id, END_WAIT)
        if ended is None:
            # The sandbox runs: a command exited 128 itself, or contact was lost.
            return result | ({"stopped": True} if None in codes else {})
        if ended != TIMEOUT_EXIT_CODE:
            notify(
                f"{name} ({sandbox_id}) ended with exit code {ended}, not at its "
                "timeout, so nothing was resumed. Continue with agentix sandbox "
                f"resume {name}."
            )
            return result | {"stopped": True}
        if restores == MAX_RESUMES:
            notify(
                f"{name} reached its timeout again, after {MAX_RESUMES} resumes, "
                f"so agentix stops here. Continue with agentix sandbox resume {name}, "
                f"or start it with a longer timeout."
            )
            return result | {"stopped": True}
        restores += 1
        sandbox_id, snapshot, _, resumed = await _restore(session, name)
        if not resumed:
            notify(
                f"{name} reached its timeout, but its commands had finished. "
                f"Restored it from snapshot {snapshot['id']} as {sandbox_id}."
            )
            return result | {"sandbox_id": sandbox_id, "resumes": restores}
        for run, was in resumed:
            notify(
                _resumed(
                    name,
                    sandbox_id,
                    snapshot,
                    run,
                    was,
                    f"resume {restores} of {MAX_RESUMES}",
                )
            )
        runs = [run for run, _ in resumed]


async def _run_once(
    session, name: str, sandbox_id: str, run: Run, notify: Notify, output: Output
) -> int | None:
    """Run `run` under the wrapper. None if the command's end was not seen."""
    from . import sandbox
    from .session import describe

    args = ["sh", "-c", WRAPPER, "agentix-run", RUNS_DIR, run["id"], json.dumps(run)]
    try:
        result = await sandbox.execute(session, sandbox_id, [*args, *run["command"]])
    except Exception as error:
        # A sandbox that is ending refuses commands; the watcher sees that next.
        notify(
            f"Lost `{shlex.join(run['command'])}` in {name} ({sandbox_id}): "
            f"{describe(error)}. It may still be running there. If {name} reaches "
            f"its timeout, agentix resumes it; if this command has stopped "
            f"watching, continue it with agentix sandbox resume {name} once "
            f"{name} has stopped."
        )
        return None
    output(run, dict(result))
    return result["exit_code"]


async def _exit_code(session, sandbox_id: str, wait: float) -> int | None:
    """The exit code of a sandbox, waiting up to `wait` seconds for it to end."""
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        async with session.sandbox(sandbox_id) as handle:
            code = await handle.poll.aio()
        if code is not None or asyncio.get_running_loop().time() >= deadline:
            return code
        await asyncio.sleep(POLL_INTERVAL)


async def _restore(session, target: str):
    """Restore `target` from its latest snapshot, with the runs it cut off.

    Returns the new sandbox ID, the snapshot, the name, and for each run the
    run as it continues and the command that was cut off.
    """
    from . import lifecycle, sandbox, store

    for attempt in range(RESTORE_ATTEMPTS):
        try:
            started = await lifecycle.restore(session, target)
            break
        except OperationFailed:
            # Modal may still be saving the snapshot.
            if attempt == RESTORE_ATTEMPTS - 1:
                raise
    name, sandbox_id = started["name"], started["sandbox_id"]
    record = await store.load(session, name)
    [snapshot] = [s for s in record["snapshots"] if s["id"] == started["snapshot"]]
    found = await sandbox.execute(
        session, sandbox_id, ["sh", "-c", READ_MARKERS, "agentix-runs", RUNS_DIR]
    )
    runs = []
    for line in found["stdout"].splitlines():
        try:
            marker = json.loads(line)
        except ValueError:
            continue
        # Markers of earlier sandboxes were restored without being resumed.
        if (
            not isinstance(marker, dict)
            or marker.get("sandbox_id") != snapshot["sandbox_id"]
        ):
            continue
        run: Run = {
            **marker,
            "sandbox_id": sandbox_id,
            "command": marker["resume"],
            "resumes": marker.get("resumes", 0) + 1,
        }
        runs.append((run, marker["command"]))
    return sandbox_id, snapshot, name, runs
