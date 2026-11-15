# Resuming work after a sandbox times out

Modal ends a sandbox at its timeout without waiting for the commands running
in it, and saves an exit snapshot. A long agent run started with
`sandbox exec` dies there. This note describes how agentix continues such a
run in a sandbox restored from that snapshot.

## What it looks like

```sh
uv run agentix sandbox exec dev 'claude -p "Fix the failing tests"' --resume
uv run agentix sandbox exec dev './build.sh' --resume='./build.sh --continue'
uv run agentix sandbox resume dev
```

## Opt-in

Nothing is resumed unless the command was started with `--resume`.

- `--resume` alone looks up a resume rule for the command (below). A command
  with no rule is refused before it starts, with the list of known rules.
- `--resume='COMMAND'` names the command that continues it, for any command.
  The user wrote it, so it is not a guess.

The choice is per command: a sandbox has nothing to resume of its own, and
two commands in one sandbox can need different handling.

## Resume rules

A rule is data in `src/agentix/resume.py`: the words a command starts with,
and the words that replace them to continue it. The rest of the command is
kept, so flags such as `--model` survive.

| Rule | Command starts with | Continues as |
| --- | --- | --- |
| claude | `claude` | `claude --continue ...` |
| codex | `codex exec` | `codex exec resume --last ...` |
| pi | `pi` | `pi --continue ...` |

The first word matches by its base name, so `/root/.nix-profile/bin/claude`
is `claude`, and is kept as given in the command that continues. A command run through `sh -c`, `nix shell`, or anything else
has no rule; pass `--resume='COMMAND'`.

`claude --continue` takes the latest conversation in the working directory,
which is the sandbox's workdir in both sandboxes. Keeping the rest of the
command keeps the prompt too: `claude --continue -p "Fix the failing tests"`
gives the agent its prompt again, after its whole history. To send another
prompt, say `--resume='claude --continue -p "Carry on where you left off."'`.

Codex and pi are not in the image yet (task more-harnesses), so their rules
are untried: that task should run one timeout through each.

The resume command is worked out when the command starts and printed then,
so the user sees what a timeout will lead to before it happens. It is
stored with the run, so changing a rule later does not change a run that
has started.

## How a running command is recorded

`--resume` runs the command through a small `sh` wrapper in the sandbox.
The wrapper writes a marker file, runs the command, and removes the marker
when the command exits, whatever its exit code:

```text
/root/.local/state/agentix/runs/RUN_ID.json
{"id": "...", "sandbox_id": "sb-...", "command": [...], "resume": [...],
 "started_at": "...", "resumes": 0}
```

`/root` is in the exit snapshot (`/run` is not), so a marker in a snapshot
means the command was still running when that sandbox ended. `sandbox_id`
names the sandbox that ran it. A restored sandbox holds the markers of
earlier sandboxes too, if a run was restored without being resumed; only
markers whose `sandbox_id` is the sandbox that left the snapshot count. The
others are ignored.

Nothing is added to the records on the Volume: the snapshot itself says
what was running, and the record already says which sandbox left each
snapshot.

## Who notices the timeout

The host command is not always running, and nothing in Modal calls back when
a sandbox ends. So there are two triggers, sharing one code path:

1. **The `sandbox exec --resume` that started the command.** It waits for
   the command, as `exec` always does. When Modal ends the sandbox at its
   timeout, the exec returns (Modal reports exit code 128 for it) and the
   sandbox's exit code becomes 124 a moment later. The watcher then restores
   the name from its latest snapshot, reads the markers, and starts the
   resume command of each unfinished run, wrapped again, so a second
   timeout is handled the same way. It stops after 3 resumes.
2. **`sandbox resume NAME`**, by hand, for when the host was not watching:
   the laptop slept, the terminal closed, or the watcher hit its limit. The
   name must be stopped. It restores the latest snapshot, resumes what the
   markers list, and then watches as in 1. With no marker it is a plain
   restore, and says so.

A watcher that sees the sandbox end some other way (`sandbox down` from
another terminal, the main process dying) does not resume; it says how to,
with `sandbox resume NAME`.

Rejected for now: a Modal Function on a schedule that checks every record
and resumes on its own. It works with no host at all, but it needs a
deployed app, its own credentials, and somewhere to show output, and a run
nobody asked to resume would cost money with no one watching. It fits the
detached-runs task, which needs the same thing: a run whose output lives in
Modal, not in a host process. The markers and rules here would serve it
unchanged.

## What the user is told

On standard error, as it happens:

- at the start: the command, the rule or `--resume` command, and what a
  timeout leads to;
- at each resume: that NAME reached its timeout, the snapshot and the new
  sandbox ID, and the exact command that continues the run, with its count
  (resume 1 of 3);
- at the end: why it stopped resuming, if it did, and how to continue.

The output of each attempt is printed when it ends, so output written before
the timeout is not lost.

## Failure modes

- **Host process dies.** The command keeps running in the sandbox until it
  ends or the timeout ends the sandbox (checked: its marker stays and its
  work goes on after the `exec` is killed). Nobody restores it; run
  `sandbox resume NAME` within the 30 days Modal keeps the snapshot.
- **Command finishes just as the timeout hits.** If the wrapper removed its
  marker before the snapshot, nothing is resumed. If not, the command runs
  its resume form once more; for an agent that continues a finished
  conversation, that is a short no-op turn.
- **Work since the last write is lost.** The snapshot holds the files as
  they were at the timeout. An agent's conversation log is written turn by
  turn, so at most the turn in flight is lost; the resumed agent may redo
  it.
- **The resume command is wrong** (a rule out of date, a harness without a
  continue feature). It fails like any command, and its error is printed.
  It never runs something the user did not start or name.
- **The snapshot is not saved yet.** Restore waits up to a minute, and the
  watcher retries twice more.
- **Two watchers of one name.** Each run in a snapshot is resumed by the
  first watcher that restores the name; the other is refused by Modal (one
  running sandbox per name), reports it, and stops.
- **Sandboxes without a snapshot.** Ephemeral sandboxes, sandboxes without a
  name, and names started with `--no-snapshot` are refused at the start.
- **Endless timeouts.** A command that never finishes within the timeout is
  resumed at most 3 times per watcher; `sandbox resume` starts a new count.
- **Processes the command started** (servers, background jobs) are not
  restored; only the command itself is continued.
