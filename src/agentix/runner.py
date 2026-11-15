"""The deployed Modal app that runs detached commands. See docs/detached-runs.md.

`agentix runner deploy` deploys it. Each detached run is one call of `run`,
started with `.spawn()`; its FunctionCall ID is the run ID. The call starts
the command in the sandbox, unless a call for the same run started it
already, and then follows its output until it exits. What it prints is the
call's log, and what it returns is the call's result, so both outlive the
process that started the run.

This module is imported in the function's container, which has Modal and the
standard library and nothing else of agentix: keep its imports so.
"""

import os
import sys

import modal

# Kept apart from the markers of `sandbox exec --resume`, in runs/.
RUNS_DIR = "/root/.local/state/agentix/detached"
# A call follows the command for at most this long, Modal's limit, which is
# also the longest a sandbox can run.
CALL_TIMEOUT = 24 * 60 * 60
# The wrapper prints this, and exits with this code, if the command ended
# without writing its exit code, for example because it was killed.
NO_EXIT_CODE = 255

# Runs in the sandbox as `sh -c WRAPPER agentix-run DIR COMMAND...`. The first
# call makes DIR and starts COMMAND in the background, with its output in
# DIR/output.log and its exit code in DIR/exit afterwards. Every call then
# prints the log from its start until COMMAND has ended. So a call that Modal
# retries, say after a preemption, follows the run instead of starting it
# again, and killing a call leaves COMMAND running.
WRAPPER = f"""set -u
dir=$1
shift
mkdir -p "${{dir%/*}}"
if mkdir "$dir" 2>/dev/null; then
  : > "$dir/output.log"
  nohup sh -c '"$@" >> "$0/output.log" 2>&1 < /dev/null
code=$?
printf "%s\\n" "$code" > "$0/exit.tmp" && mv "$0/exit.tmp" "$0/exit"' \\
    "$dir" "$@" > /dev/null 2>&1 &
  printf '%s\\n' "$!" > "$dir/pid.tmp" && mv "$dir/pid.tmp" "$dir/pid"
fi
while [ ! -e "$dir/pid" ]; do sleep 0.2; done
tail -n +1 -f --pid="$(cat "$dir/pid")" "$dir/output.log"
if [ -e "$dir/exit" ]; then
  exit "$(cat "$dir/exit")"
fi
echo "agentix: the command ended without an exit code" >&2
exit {NO_EXIT_CODE}
"""

APP_NAME = os.environ.get("AGENTIX_RUNNER_APP") or "agentix-runner"
FUNCTION_NAME = "run"

app = modal.App(APP_NAME)


@app.function(
    image=modal.Image.debian_slim(),
    cpu=0.125,
    memory=256,
    timeout=CALL_TIMEOUT,
    name=FUNCTION_NAME,
)
def run(sandbox_id: str, args: list[str]) -> dict:
    """Run `args` in the sandbox `sandbox_id`, and follow it.

    The run is named by this call's ID, so a retried call follows it.

    Returns the exit code. When the sandbox ends first, Modal reports 128 or
    the exec fails, and `sandbox_ended` is true.
    """
    run_id = modal.current_function_call_id()
    sandbox = modal.Sandbox.from_id(sandbox_id)
    try:
        process = sandbox.exec(
            "sh", "-c", WRAPPER, "agentix-run", f"{RUNS_DIR}/{run_id}", *args
        )
        for chunk in process.stdout:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        exit_code = process.wait()
        sys.stderr.write(process.stderr.read())
    except modal.exception.Error as error:
        print(f"agentix: lost the sandbox: {error}", file=sys.stderr)
        return {"exit_code": None, "sandbox_ended": True, "error": str(error)}
    ended = sandbox.poll() is not None
    return {"exit_code": exit_code, "sandbox_ended": ended}
