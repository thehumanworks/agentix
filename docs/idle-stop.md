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

A restored sandbox keeps the idle stop its name had. `sandbox restore NAME
--idle-stop=1h` (or `off`) changes it, for that sandbox and later restores.

## What counts as use

The sandbox is idle while none of these runs in it:

- an SSH session: `sandbox shell`, plain `ssh`, scp, SFTP, rsync or git
  over SSH. Over OpenSSH a session counts however short it is. Over
  Tailscale SSH it counts only while it lasts, as the watcher sees it at one
  of its looks: a session shorter than the poll (5 to 30 s), such as a
  quick `ssh dev cmd`, can be missed, so drive a sandbox with many short
  commands over OpenSSH or `sandbox exec` instead;
- a command started through agentix: `sandbox exec`, `sandbox exec --resume`
  and `sandbox exec --detach`, and anything they start that is still running;
- any other process left running, such as a server started in a session that
  has ended.

Being connected counts, not typing: an open but quiet SSH session keeps the
sandbox up. Leaving a process running keeps it up too, so stop background
servers when you are done. Port 22 is public for the OpenSSH tunnel, so a
scanner's connection that never logs in also counts, until sshd drops it
after its login grace time (120 s by default).

## Design

**Where the check runs.** Inside the sandbox, as its main command. The host
that ran `sandbox up` may be asleep or offline, so it cannot be the one that
notices. Modal ends a sandbox when its main command exits, and saves the exit
snapshot however a sandbox ends (checked: a main command that exits saves a
snapshot that restores, and Modal reports its exit code). So the watcher only
has to exit; no stop call, credentials or Modal client are needed inside the
sandbox.

**The watcher.** Without an idle stop the start command is as it was,
ending in `exec sleep infinity`. With one, it first makes the flag file
`/run/agentix/idle-stop`, then ends in `exec sh -c WATCHER` (the script is
in `src/agentix/idle.py` and is passed in the start command, so the image is
unchanged and needs no rebuild). Every 5 to 30 seconds (a sixth of the idle
stop) it looks, and a look is busy if any of these holds:

- **a mark is there.** Every command agentix starts in a sandbox
  (`sandbox exec` with or without `--resume`, detached runs, the clone of
  `--repo`) runs through a small wrapper (`idle.EXEC_WRAPPER`, and the same
  line in the runner's wrapper) that, while the flag exists, writes
  `/run/agentix/used.PID` and then `exec`s the command, so its output, exit
  code and PID are unchanged. Its environment is too, except that bash, the
image's `sh`, adds `SHLVL=0` at every exec (the wrapper drops the `PWD` the
shell adds). A first word that starts with `-` is refused with exit code 127,
as Modal refused it before (bash's `exec` would read it as an option). The watcher removes the marks it sees. So a
  command of any length is seen, also one that ends before the next look.
  (A fork counter, such as `processes` in `/proc/stat`, was the other
  option; it also counts threads, so a daemon such as `tailscaled` that
  starts threads now and then would keep the sandbox up for good.)
- **OpenSSH logged a login since the last look.** sshd writes its log to
  `/root/.local/state/agentix/openssh/sshd.log` (`-E` in
  `scripts/openssh-ssh.sh`), one line starting `Accepted METHOD for` per
  login, for every kind of session: a shell, `ssh HOST CMD`, scp, SFTP,
  rsync, git. The watcher counts those lines with `grep -c` at each look; a
  new one is use, so a session of any length is seen. A client cannot write
  the start of a log line, so a scanner cannot fake one; its connections
  that never log in are not counted. This was chosen over a `~/.ssh/rc` that
  leaves a mark: with an rc file sshd no longer runs `xauth` for X11
  forwarding itself, anything the file prints corrupts scp and SFTP, it would
  live in `/root` and so in every snapshot, and `PermitUserRC no` turns it
  off. Tailscale SSH is served by `tailscaled` and logs nothing comparable,
  so it is not covered.
- **a process runs that counts.** It reads `/proc`. Each process counts as
  use except: PID 1 (Modal's agent), kernel threads, zombies, the watcher
  and what it starts, the SSH listeners (an `sshd` that PID 1 holds, and
  `tailscaled` itself, but not its `be-child` session processes), the agents
  a login can leave behind (`ssh-agent`, `gpg-agent` and the like), and
  Modal's readiness probe. Names are matched on both the kernel's name and
  the base name of argv[0], without what Nix's `wrapProgram` adds: on Linux
  nixpkgs wraps `tailscaled`, which then runs as `.tailscaled-wra`. A
  process whose command line cannot be read counts, so a fault keeps the
  sandbox up rather than stopping it.

Idle time counts from the first look that sees nothing. A mark or a login
is seen at the look after it, so the sandbox stops between the idle stop
and two polls later after the last use, never sooner (seen: 80 s after the
last of a series of 0.1 s ssh and scp sessions, with a 1m idle stop and a
10 s poll). Then it exits with code 75. The clock is `/proc/uptime`.

This covers each kind of use without asking it to report in. Checked in a
sandbox: a `sandbox exec` command, and so the wrappers of `--resume` and of
detached runs, runs as a child of PID 1; an OpenSSH session runs as
`sshd-session` under the listener. Tailscale SSH sessions run under
`tailscaled be-child ssh` (unchecked here, since Tailscale needs the
1Password secret). The resume markers in `runs/` and the detached run files
in `detached/` are not read: a snapshot keeps the markers of runs cut off in
an earlier sandbox, which would keep a restored sandbox up for good, and the
processes they describe are counted anyway. The marks and the flag are in
`/run`, a tmpfs, so no snapshot keeps them.

**Recording it.** The host learns of the stop on its next command that reads
the record (`ls`, `restore`, `snapshots`, ...), as it learns of a timeout.
An exit code of 75 from a sandbox with an idle stop is recorded as a
snapshot of kind `idle`. The idle stop, in seconds, is kept on each
sandbox's entry in the record, not in the name's options: a restore takes
the one of the sandbox that started last, and `restore --idle-stop` sets a
new one that way, which merges cleanly with other copies of the record. An
older agentix, which passes the options on as they are, restores such a
name without an idle stop instead of failing. A sandbox without a record
(an `--ephemeral` one) carries the idle stop in its `agentix-idle-stop` tag
for `sandbox ls`. An ephemeral sandbox has no snapshot: its idle stop just
ends it.
