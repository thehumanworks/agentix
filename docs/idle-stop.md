# Idle stop

`agentix sandbox up --name=NAME --idle-stop=20m` starts a sandbox that stops
by itself once nobody has used it for 20 minutes. It stops the way `sandbox
down` stops it: Modal saves its exit snapshot, and `sandbox restore NAME`
continues from it. Set a default for every `sandbox up` in the config file:

```toml
[sandbox.up]
idle_stop = "20m"
```

`--idle-stop=off` turns the default off for one sandbox. The duration takes
the units of `sandbox prune --older-than` (`90m`, `1h30m`, ...) and is at
least `1m`. `sandbox ls` shows each sandbox's setting in `idle_stop`.

## What counts as use

The sandbox is idle while none of these runs in it:

- an SSH session: `sandbox shell`, plain `ssh`, scp or SFTP, over OpenSSH or
  Tailscale SSH;
- a command started through agentix: `sandbox exec`, `sandbox exec --resume`
  and `sandbox exec --detach`, and anything they start that is still running;
- any other process left running, such as a server started in a session that
  has ended.

Being connected counts, not typing: an open but quiet SSH session keeps the
sandbox up. Leaving a process running keeps it up too, so stop background
servers when you are done.

## Design

**Where the check runs.** Inside the sandbox, as its main command. The host
that ran `sandbox up` may be asleep or offline, so it cannot be the one that
notices. Modal ends a sandbox when its main command exits, and saves the exit
snapshot however a sandbox ends (checked: a main command that exits saves a
snapshot that restores, and Modal reports its exit code). So the watcher only
has to exit; no stop call, credentials or Modal client are needed inside the
sandbox.

**The watcher.** Without an idle stop the main command ends in `exec sleep
infinity`, as before. With one, it ends in `exec sh -c WATCHER` (the script
is in `src/agentix/idle.py` and is passed in the sandbox's start command, so
the image is unchanged and needs no rebuild). Every 5 to 30 seconds (a sixth
of the idle stop) it reads `/proc`. Each process counts as use except: PID 1
(Modal's agent), kernel threads, zombies, the watcher and what it starts, the
SSH listeners (an `sshd` that PID 1 holds, and `tailscaled` itself, but not
its `be-child` session processes), the agents a login can leave behind
(`ssh-agent`, `gpg-agent` and the like), and Modal's readiness probe. Idle
time counts from the first look that sees nothing, so the sandbox stops
between the idle stop and one poll later after the last use ended, never
sooner. Then it exits with code 75. The clock is
`/proc/uptime`.

This covers each kind of use without asking it to report in. Checked in a
sandbox: a `sandbox exec` command, and so the wrappers of `--resume` and of
detached runs, runs as a child of PID 1; an OpenSSH session runs as
`sshd-session` under the listener. Tailscale SSH sessions run under
`tailscaled be-child ssh` (unchecked here, since Tailscale needs the
1Password secret). The resume markers in `runs/` and the detached run files
in `detached/` are not read: a snapshot keeps the markers of runs cut off in
an earlier sandbox, which would keep a restored sandbox up for good, and the
processes they describe are counted anyway.

A command that starts and ends between two looks is not seen. That only
matters for commands of a few seconds near the end of the idle time.

**Recording it.** The host learns of the stop on its next command that reads
the record (`ls`, `restore`, `snapshots`, ...), as it learns of a timeout.
An exit code of 75 from a sandbox with an idle stop is recorded as a
snapshot of kind `idle`. The record's options keep `idle_stop` in seconds,
so a restored sandbox stops when idle too; a sandbox without a record (an
`--ephemeral` one) carries it in its `agentix-idle-stop` tag for `sandbox
ls`. An ephemeral sandbox has no snapshot: its idle stop just ends it.
