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
from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from .errors import InvalidRequest, OperationFailed

# How each harness continues its latest session: the words a command starts
# with, and the words that replace them. The rest of the command is kept, so
# its flags and prompt are given again. The first word matches by base name
# and is kept as given, so /opt/bin/claude continues as /opt/bin/claude.
# codex and pi are not in the image yet, so their rules are untried.
RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "claude": (("claude",), ("claude", "--continue")),
    "codex": (("codex", "exec"), ("codex", "exec", "resume", "--last")),
    "pi": (("pi",), ("pi", "--continue")),
}

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


def plan(argv: Sequence[str], resume: object) -> tuple[list[str], str]:
    """The command that continues `argv`, and where it came from.

    `resume` is True to use the rule for `argv`, or the command, as one
    string, that continues it. A command with no rule is refused.
    """
    if resume is not True:
        words = shlex.split(str(resume))
        if not words:
            raise InvalidRequest("--resume='' names no command.")
        return words, "--resume"
    for rule, (start, then) in RULES.items():
        if (
            len(argv) >= len(start)
            and os.path.basename(argv[0]) == start[0]
            and tuple(argv[1 : len(start)]) == start[1:]
        ):
            # The command keeps its first word, so a path stays the same.
            return [argv[0], *then[1:], *argv[len(start) :]], f"the {rule} rule"
    known = ", ".join(
        f"{' '.join(start)} -> {' '.join(then)}" for start, then in RULES.values()
    )
    raise InvalidRequest(
        f"No resume rule for `{shlex.join(argv)}`, so it would not be resumed. "
        f"Rules: {known}. Say how it continues with --resume='COMMAND'."
    )


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
    then, how = plan(argv, resume)
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
        "command": list(argv),
        "resume": then,
        "started_at": store.now(),
        "resumes": 0,
    }
    notify(
        f"Running `{shlex.join(argv)}` in {name} ({sandbox_id}). If {name} "
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
            *(_run_once(session, sandbox_id, run, output) for run in runs)
        )
        cut_off = any(code in (None, CUT_OFF_EXIT_CODE) for code in codes)
        ended = await _exit_code(session, sandbox_id, END_WAIT if cut_off else 0)
        result = {
            "name": name,
            "sandbox_id": sandbox_id,
            "resumes": restores,
            "runs": [
                {"command": run["command"], "exit_code": code}
                for run, code in zip(runs, codes)
            ],
        }
        if ended is None:
            return result
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


async def _run_once(session, sandbox_id: str, run: Run, output: Output) -> int | None:
    """Run `run` under the wrapper. None if the sandbox could not be reached."""
    from . import sandbox

    args = ["sh", "-c", WRAPPER, "agentix-run", RUNS_DIR, run["id"], json.dumps(run)]
    try:
        result = await sandbox.execute(session, sandbox_id, [*args, *run["command"]])
    except Exception:
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
