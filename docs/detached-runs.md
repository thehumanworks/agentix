# Detached runs: leave a command running and check on it later

`sandbox exec` waits for its command, and its output is lost if the terminal
closes. A detached run starts the command, prints a run ID at once, and
leaves it running; any later process can follow its output or wait for its
exit code.

```sh
uv run agentix runner deploy                       # once per Modal workspace
uv run agentix sandbox exec dev 'make test' --detach   # prints fc-...
uv run agentix sandbox logs fc-...                 # output from the start, until it ends
uv run agentix sandbox wait fc-...                 # prints the exit code and exits with it
uv run agentix sandbox runs dev                    # the runs of dev, with their state
uv run agentix sandbox runs dev --clean            # forget the runs that have ended
```

## Shape

`--detach` is a flag of `sandbox exec`, like `--resume`: one verb runs a
command, and a flag says who watches it. `--detach` and `--resume` do not
go together yet (see the last section). The command is one string, split as
`exec` splits it.

## App and function

`src/agentix/runner.py` defines a Modal app, `agentix-runner`
(`AGENTIX_RUNNER_APP` overrides the name), with one function, `run(sandbox_id,
args)`: a small debian-slim container, 0.125 CPU and 256 MiB, with a 24-hour
timeout (Modal's limit, and the longest a sandbox lives).

`exec --detach` looks the function up with `Function.from_name`, starts it
with `.spawn(sandbox_id, args)`, and prints the FunctionCall ID. That ID is
the run ID. Nothing in agentix has to map one to the other:

- `sandbox logs RUN` is `FunctionCall.from_id(RUN).logs.stream()`. Checked
  against Modal 1.6.4: it replays the call's log from its start, then
  follows it, and stops when the call has ended. Modal's own messages
  (source `system`) are dropped.
- `sandbox wait RUN` is `FunctionCall.from_id(RUN).get()`, which returns
  `{"exit_code": N, "sandbox_ended": bool}`. A run whose sandbox ended first
  (`sandbox down`, the timeout) reports exit code 128, or none if the exec
  failed, and `wait` exits with 128 and says so.
- `sandbox runs` polls each run with `get(timeout=0)`. A call that has not
  ended raises the built-in `TimeoutError`, which is how "running" is told.

## Credentials and reaching the sandbox

None are added. A Modal function's container is already signed in to the
workspace that deployed it, so `modal.Sandbox.from_id(sandbox_id)` and
`sandbox.exec(...)` work there as they do on the laptop. The function gets
no secrets: the command runs in the sandbox, with the sandbox's own
environment and secrets, exactly as `sandbox exec` runs it.

## What runs in the sandbox, and where output lives

The function execs a short `sh` wrapper (`runner.WRAPPER`) in the sandbox,
in `/root/.local/state/agentix/detached/RUN_ID/` (apart from the markers of
`exec --resume` in `.../runs/`):

1. If the directory does not exist yet, it makes it and starts the command
   in the background under `nohup`, with standard output and error together
   in `output.log`, and the exit code written to `exit` afterwards.
2. Every call then prints `output.log` from its start with
   `tail -f --pid=PID` until the command has ended, and exits with its code.

So the output lives in three places, each of which outlives the client:

- the **function call's log** in Modal, which `logs` reads. It does not
  depend on the sandbox still running.
- the **function call's result** in Modal, which `wait` reads.
- **`output.log` and `exit` in the sandbox**, which also go into a named
  sandbox's snapshot. They are the source the function reads, and they make
  the call safe to retry: a call Modal retries after a preemption follows
  the command instead of starting it a second time, and a killed call
  leaves the command running.

Standard error is merged into the one log, in order; `logs` prints it on
standard output. Modal keeps call logs and results for a limited time, set
by Modal and not checked here; after it, `wait` says the result has expired
and `runs` shows `expired`, while `output.log` stays in the sandbox.

## Run records, listing, and cleanup

Modal cannot list the calls of a function by sandbox, so `exec --detach`
writes one file per run on the existing record Volume
(`AGENTIX_STORE`, default `agentix-sandboxes`):
`runs/SANDBOX_ID/RUN_ID.json`, with the sandbox, the name it was started
under, the command, and the start time. It is written once and never
changed, so the merge rules of `records/` are not needed. The state is not
stored; `runs` asks Modal each time.

`sandbox runs` lists every run; `sandbox runs SANDBOX_ID` the runs of one
sandbox; `sandbox runs NAME` the runs of every sandbox the name has had.
Each line has `state`: `running`, `exited` (with `exit_code`), `sandbox
ended`, `failed` (the call itself failed), or `expired`.

Cleanup is explicit: `sandbox runs [TARGET] --clean` forgets every run of
TARGET (or every run) that is no longer running. It deletes its file on the
Volume and, if its sandbox still runs, its directory with the log in the
sandbox. Runs that still run are kept. Nothing is cleaned on its own: the
files are small, and the log in a running sandbox is the user's to keep
until they say so. A sandbox that ends takes its run directories with it
(except in its snapshot); Modal drops the call logs and results on its own
schedule.

## Deploy

Explicit: `agentix runner deploy` deploys the app with `app.deploy()`.
Running it again redeploys the same code and changes nothing else, so it is
safe to run after every upgrade of agentix; the wrapper and the function
must match the client, so redeploy after pulling changes to `runner.py`.
`exec --detach` never deploys by itself: deploying changes the workspace,
and a missing app is reported with the command that deploys it.

Cost: a deployed app with no `min_containers` costs nothing while idle.
Each run keeps one function container (0.125 CPU, 256 MiB) going for as
long as the command runs, about a cent an hour at Modal's list prices, on
top of the sandbox itself. The first deploy builds the small debian-slim
image once.

## Relation to `sandbox exec --resume`

The two solve different halves. `--resume` (docs/resume.md) continues a
command after its sandbox times out, but only while the host `exec` watches.
A detached run is watched by Modal, not by the host, but does nothing when
the sandbox ends, beyond reporting it.

They share no code yet, on purpose: this change touches neither
`resume.py` nor the records. docs/resume.md rejected a deployed watcher for
now because it needs a deployed app and somewhere to show output; this
task adds both. A later step can let `run` restore and resume: when its
exec ends with 128 and the sandbox reached its timeout, it would call
`resume.resume(...)` from the function and keep printing to the same call
log, so `--detach --resume` would survive both the host and the timeout.
The markers and rules of `resume.py` work unchanged for that. It is left
out here because a run nobody watches that restores sandboxes costs money
without anyone seeing it, which is a decision for the user.

## Limits

- A run lasts at most 24 hours, the function's timeout. The command itself
  runs on until it or its sandbox ends, but `wait` then reports the call's
  timeout.
- There is no `stop RUN` yet. `FunctionCall.cancel()` would only stop the
  follower; the command runs on in the sandbox. Stop it from a shell, or end
  the sandbox.
- If `exec --detach` dies between `.spawn()` and writing the run file, the
  run goes on but `runs` does not list it; its ID was not printed either.
