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

A rule is data in `src/agentix/resume.py` (`RULES`): the words a command
starts with, words added when it starts, words added when it continues,
where its prompt is, and words that make the rule refuse it.

| Rule | Starts as | Continues as |
| --- | --- | --- |
| claude | `claude --session-id UUID -p TASK ...` | `claude --resume UUID -p CONTINUE ...` |
| codex | `codex exec TASK OPTIONS` | `codex exec OPTIONS resume --last CONTINUE` |
| pi | `pi --session-id UUID -p TASK ...` | `pi --session-id UUID -p CONTINUE ...` |

The other flags, such as `--model`, are kept. For codex they move before
`resume`: `codex exec resume` refuses most options of `codex exec`, such as
`-s` and `-C` (checked against codex 0.167.1), and takes them before the
subcommand. The first word matches by its
base name, so `/root/.nix-profile/bin/claude` is `claude`, and is kept as
given. A command run through `sh -c`, `nix shell`, or anything else has no
rule; pass `--resume='COMMAND'`.

**The task is not sent twice.** CONTINUE is a neutral prompt: the sandbox
was restored, carry on where you left off, and check what is done before
repeating a step. Sending the task again would ask the agent to do it again,
and a step with side effects, such as opening a pull request or pushing,
could happen twice. The prompt is the argument right after `-p`/`--print`
(right after `codex exec` for codex); a command whose prompt is elsewhere
(`claude "TASK" -p`) is refused rather than guessed at. `--resume='...'`
sends a prompt of the user's own.

**Each run continues its own conversation.** `claude --continue` and
`codex exec resume --last` take the latest conversation in the working
directory, which can be another run's, or an interactive `claude` the user
started over `sandbox shell`. So the claude rule gives each run a new
session ID with `--session-id` and continues exactly that one with
`--resume`, which keeps the ID (checked against claude 2.1.322's `--help`;
`--fork-session` would make a new one). A claude command that names a
session itself (`-c`, `-r`, `--resume`, `--session-id`, `--fork-session`) is
refused by the rule. pi 1.5.2's `--session-id ID` opens the workdir's
session with that ID, or creates it, so the pi rule passes the same new ID
when it starts and when it continues; a pi command with `-c`, `-r`,
`--session`, `--session-id`, `--fork`, or `--no-session` is refused, and
so is one with more than the prompt to say: pi takes several messages and
`@file` arguments, and only the prompt is replaced by CONTINUE, so
`pi -p @spec.md "do X"` and `pi -p A B` are refused rather than sent again.
The rules know which options take a value (`--model m`), so a value is
neither a message nor a refused word: `codex exec TASK -m review` is fine.
`codex exec` cannot be given a session ID when it starts, so codex continues
the latest session in the workdir (`--last` looks only at sessions started
there): run one resumable codex run per workdir, and nothing else of codex
there while it runs. The codex rule refuses `codex exec resume`, `fork`,
`review`, `--ephemeral` (no session is kept), and `-i`/`--image`, which
takes several files and would take `resume` for one.

Both rules were tried with the real CLIs against a stand-in model server:
the start form, an unrelated run in the same directory, then the continue
form. pi continued its own session; codex continued the unrelated one, as
`--last` says. A real timeout with a real model has not been run through
either.

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

`command` is what runs now, with its session ID; `resume` is what continues
it, with the same ID.

The command's input is `/dev/null`. Nothing writes to it, and `codex exec`
and `pi -p` read an open input until it ends, so with the input of the exec
they would wait there until the timeout, each time they were resumed.
`claude -p` with a prompt does not read it. A command that needs input reads
a file instead.

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
   sandbox's exit code becomes 124 a moment later. A command that exits by
   itself, with any other code, is done: nothing is checked or restored.
   After a 128, the watcher restores
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
  marker before the snapshot, nothing is resumed. If not, the resume form
  runs once more on a finished task. With the rules' neutral prompt the
  agent is asked to check what is done, not to do the task again, but it is
  still a new agent turn and may act: a step whose result did not reach the
  snapshot (a pull request opened in the last seconds, say) can be repeated.
  The same holds for any resume: work done after the last file write before
  the timeout is not in the snapshot. Prompts that ask for steps with side
  effects should say to check first ("open a pull request unless one is
  open").
- **Another conversation in the workdir.** For codex, a second run or an
  interactive session in the same workdir can be the one that is continued;
  see above. claude and pi runs are tied to their session ID.
- **Contact lost while the sandbox runs.** If an `exec` fails for another
  reason than the sandbox ending, the watcher prints the error and says the
  command may still be running; it does not restore anything.
- **Work since the last write is lost.** The snapshot holds the files as
  they were at the timeout. An agent's conversation log is written turn by
  turn, so at most the turn in flight is lost; the resumed agent may redo
  it.
- **The resume command is wrong** (a rule out of date, a harness without a
  continue feature, a session that claude cannot find). It fails like any command, and its error is printed.
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
