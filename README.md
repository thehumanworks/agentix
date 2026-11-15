# Agentix

Agentix is a command line tool that runs development sandboxes for coding
agents on [Modal](https://modal.com). It builds one image from the Nix and
Home Manager configuration in `nix/`, publishes it on Modal, and starts VM
sandboxes from it. A sandbox has a name that outlives it: when it stops,
Modal saves its filesystem, and `sandbox restore` continues from there.
`sandbox shell` logs in over Tailscale or OpenSSH in one step, and 1Password
secrets are available in every session, SSH logins included.

## Requirements

- [uv](https://docs.astral.sh/uv/). It installs Python 3.14 and the
  dependencies. Run every command from a checkout of this repository: the
  command line reads `nix/` at run time.
- A Modal account, authenticated once with `uv run modal setup`.
- For secrets: a Modal secret named `1password` that holds
  `OP_SERVICE_ACCOUNT_TOKEN`, a 1Password service account token. Without it,
  start sandboxes with `--secret-name=''`; see [Secrets](#secrets).
- To log in: `ssh`, `ssh-keygen`, and `ssh-keyscan` on your computer, and
  either a tailnet that your computer is on (for the default Tailscale
  engine, which also needs the 1Password secret) or an SSH key pair (for the
  OpenSSH engine). See [SSH](#ssh).

No Nix is needed on your computer. Builds and Nix commands run in Modal
sandboxes.

## Quick start

```sh
uv run modal setup                       # once
uv run agentix image build               # once, and after changing nix/
uv run agentix sandbox up --name=dev
uv run agentix sandbox shell dev         # Tailscale; needs the secret and a tailnet
```

Without 1Password or Tailscale, use the OpenSSH engine and your
`~/.ssh/id_ed25519.pub` (or `~/.ssh/id_rsa.pub`):

```sh
uv run agentix sandbox up --name=dev --secret-name=''
uv run agentix sandbox shell dev --engine=openssh
```

`sandbox down dev` stops the sandbox and Modal saves its files;
`sandbox restore dev` continues from them.

## Commands

| Command | What it does |
| --- | --- |
| `image build [--force-build]` | Build the image from `nix/` and publish it; prints `image_id` |
| `flake update [INPUT...] [--build]` | Move pinned Nix inputs in `nix/flake.lock`; `--build` also rebuilds when one moved |
| `config show` | The defaults `sandbox up` uses, and where each comes from |
| `sandbox up [FLAGS]` | Start a sandbox; prints `sandbox_id` and `name` |
| `sandbox ls` | Every name with its current sandbox, and running sandboxes without a name |
| `sandbox exec SANDBOX 'COMMAND' [--resume[=CMD]] [--detach]` | Run one command and print its output; `--resume` continues it after a timeout; `--detach` prints a run ID and leaves it running |
| `sandbox logs RUN` / `sandbox wait RUN` | Follow a detached run's output from its start / wait for it and exit with its exit code |
| `sandbox runs [SANDBOX] [--clean]` | Detached runs and their state; `--clean` forgets those that ended |
| `sandbox sync [SANDBOX] [--pull] [--path=DIR]` | Copy this checkout's uncommitted changes into the sandbox's clone, or back with `--pull` |
| `sandbox shell [SANDBOX] [-- COMMAND]` | Start SSH if needed, then log in with `ssh`, or run COMMAND |
| `sandbox ssh start\|status\|stop SANDBOX` | Control one SSH engine; `start` prints how to connect |
| `sandbox down SANDBOX` | Stop a sandbox; for a named one, prints the snapshot Modal saved |
| `sandbox snapshot SANDBOX` | Save the filesystem of a running sandbox now |
| `sandbox snapshots SANDBOX` | The snapshots of a name, oldest first, with their expiry |
| `sandbox restore NAME [--snapshot=ID]` | Start a new sandbox under NAME from its latest or the given snapshot |
| `sandbox resume NAME` | Restore NAME and continue the `exec --resume` commands its timeout cut off |
| `sandbox purge NAME [--terminate]` | Delete every snapshot of a name, then the name |
| `sandbox prune --older-than=AGE [--keep-latest] [--name=GLOB] [--yes]` | List snapshots and stopped sandboxes older than AGE, then delete them once confirmed |
| `auth install\|uninstall\|status\|sync` | Keep Codex's and pi's sign-in files on this computer in step with 1Password; see [Codex and pi sign-in](#codex-and-pi-sign-in) |
| `cost [--json]` | This month's Modal spend: billed per app, estimated per sandbox name, image builds, snapshots; see [docs/cost.md](docs/cost.md) |
| `runner deploy` | Deploy the Modal app that runs `exec --detach` commands; safe to repeat |

Each command is `uv run agentix ...`. `SANDBOX` is a sandbox name or a
sandbox ID (`sb-...`); the snapshot commands take an ID only for a sandbox
that has a name. The command line uses
[click](https://click.palletsprojects.com/):

- A mistyped command or flag, a value of the wrong type, or a flag that
  needs a value and has none stops the command before it does anything,
  with exit code 2 and a suggestion, as in
  `No such option '--nmae'. (Did you mean one of: '--hostname', '--name'?)`.
- A flag takes its value as `--flag=VALUE` or `--flag VALUE`. Boolean flags
  are switches: `--tailscale` or `--no-tailscale`.
- Show help with `--help` (or `-h` where no flag takes `-h`), as in
  `uv run agentix sandbox up --help`.
- A result prints as `key: value` lines, a list as one JSON object per line.
- The spellings of the earlier python-fire command line still work:
  `--secret_name` for `--secret-name`, `--notailscale`, `--tailscale=False`
  (also `false`, `0`, `no`), a positional given as a flag, as
  `--sandbox-id=sb-...`, the one-letter flags Fire gave, as `-m 1024`, and
  `sandbox up -- --help`. See [docs/cli-library.md](docs/cli-library.md).

### Starting a sandbox

| `sandbox up` flag | Default | Effect |
| --- | --- | --- |
| `--name=NAME` | generated, such as `brisk-otter` | The name; see [Names](#names) |
| `--timeout=SECONDS` | `3600` | Modal ends the sandbox after this long, and saves a snapshot unless told not to |
| `--memory=MIB` | `8192` | Memory reserved for the sandbox |
| `--cpu=CORES` | Modal's default share | CPU cores reserved |
| `--workdir=PATH` | `/workdir` | Absolute path where commands and SSH logins start |
| `--repo=OWNER/NAME[@REF]` | none | Clone that GitHub repository into `/workdir/NAME` and start there; see below |
| `--secret-name=NAME` | `1password` | Modal secret to attach; `''` attaches none |
| `--image-name=NAME` | `determinate-nix:latest` | Published image to start from |
| `--tailscale` | off | Also start Tailscale SSH and print its address |
| `--hostname=NAME` | the sandbox name | Tailscale hostname; only with `--tailscale` |
| `--no-snapshot` | off | Modal saves nothing when the sandbox ends |
| `--ephemeral` | off | No name, no record, no snapshot |

Every flag above except `--name`, `--hostname`, and `--ephemeral` can get
its default from your config file instead; see
[Defaults for `sandbox up`](#defaults-for-sandbox-up).

`sandbox up` returns once the sandbox is ready for commands, about a second
after Modal accepts it. It does not build the image. Sandboxes run in
Modal's VM runtime and reserve TCP port 22 for the OpenSSH tunnel.

`--repo=OWNER/NAME[@REF]` starts the sandbox inside a clone of
`github.com/OWNER/NAME`: Git clones it in the sandbox with the sandbox's own
GitHub credentials (the image's `gh` credential helper, through the attached
secret), checks out REF (a branch, tag, or commit) if given, and runs
`direnv allow` if the repository has an `.envrc`. `/workdir/NAME` then
replaces `--workdir`, which is refused with `--repo` (a `workdir` in the
config file is ignored), so `sandbox exec`, `sandbox shell`, and a later
`sandbox restore` start there; a restore does not clone again. If the clone
fails, for a wrong name or a repository the credentials cannot read, the
sandbox is removed and Git's error is reported. A public repository clones
without a secret.

```sh
uv run agentix sandbox up --name=dev --repo=thehumanworks/agentix@main
uv run agentix sandbox shell dev   # lands in /workdir/agentix
```

### Defaults for `sandbox up`

To stop retyping the same flags, put them in `~/.config/agentix/config.toml`
(or `$XDG_CONFIG_HOME/agentix/config.toml`), under `[sandbox.up]`:

```toml
[sandbox.up]
timeout = 7200
memory = 16384
tailscale = true
```

| Key | Type | Built-in default | Flag |
| --- | --- | --- | --- |
| `image_name` | string | `"determinate-nix:latest"` | `--image-name` |
| `secret_name` | string | `"1password"`; `""` attaches none | `--secret-name` |
| `timeout` | integer, seconds | `3600` | `--timeout` |
| `memory` | integer, MiB | `8192` | `--memory` |
| `cpu` | number, cores | `0`, Modal's default share | `--cpu` |
| `workdir` | string | `""`, which is `/workdir` | `--workdir` |
| `tailscale` | boolean | `false` | `--tailscale` |
| `no_snapshot` | boolean | `false` | `--no-snapshot` |

A flag on the command line wins over the file, and the file wins over the
built-in default, also when the flag gives the built-in default: with
`timeout = 7200` in the file, `--timeout=3600` starts a sandbox for an hour.
Turn a boolean off with `--no-tailscale`, and keep the snapshot that the
file turns off with `--snapshot`. `--workdir=None` (as for any of these
flags) reads as the flag not given, so the file's value applies; use
`--workdir=` for an empty workdir, which is `/workdir`.
`--ephemeral` still saves no snapshot, whatever the file says. Only
`sandbox up` reads the file; `sandbox restore` uses the options the name was
created with.

Without the file, nothing changes. A key that is not in the table above, a
value of the wrong type, invalid TOML, or a file that cannot be read stops
`sandbox up` before anything starts, with a message that names the file and
the key. Keys are written with underscores, as help prints the flags.

`config show` prints each key with the value `sandbox up` would use and its
source, `default` or `config PATH`:

```sh
$ uv run agentix config show
{"key": "timeout", "value": 7200, "source": "config /home/me/.config/agentix/config.toml", "type": "integer"}
{"key": "memory", "value": 8192, "source": "default", "type": "integer"}
...
```

Because the flags of `sandbox up` default to the file, its help shows
`Default: None` for them; `config show` has the values.

### Running commands

`sandbox exec` takes the command as one string, splits it into words as a
shell would, and runs it without a shell. Use `bash -c` for pipes, `&&`, or
variables. It prints the command's standard output; a non-zero exit code
fails with its standard error.

```sh
uv run agentix sandbox exec dev 'nvim --version'
uv run agentix sandbox exec dev 'bash -lc "command -v uv && ls | wc -l"'
```

A long agent run can outlast the sandbox; see
[Resuming after a timeout](#resuming-after-a-timeout) for `--resume`.

`--detach` leaves the command running after `exec`, the terminal, or the
laptop goes away. It prints a run ID (`fc-...`) at once; `sandbox logs RUN`
prints the output from the start and follows it until the command ends,
and `sandbox wait RUN` prints the exit code and exits with it (128 when the
sandbox ended first). Standard output and error come as one stream.

```sh
uv run agentix runner deploy              # once per workspace; see below
run=$(uv run agentix sandbox exec dev 'make test' --detach)
uv run agentix sandbox logs "$run"
uv run agentix sandbox wait "$run"
uv run agentix sandbox runs dev           # runs of every sandbox of dev
uv run agentix sandbox runs dev --clean   # forget runs that have ended
```

A run is a call of a Modal function, `run` in the app `agentix-runner`,
which `runner deploy` deploys (set `AGENTIX_RUNNER_APP` for another name).
It costs nothing while idle, and a small container (0.125 CPU, 256 MiB)
for as long as a run goes. The function execs the command in the sandbox
and follows it; the log and exit code stay with Modal, and a copy in
`/root/.local/state/agentix/detached/RUN/` in the sandbox. Each run also
leaves a small file in `runs/` on the record Volume, so that `sandbox runs`
can list it; `--clean` removes those of runs that have surely ended, with
their logs in sandboxes that still run, and keeps runs whose state is
unknown. A run lasts at most 24 hours, there is no
`stop` yet, and `--detach` does not go with `--resume`. Design and failure
modes: [docs/detached-runs.md](docs/detached-runs.md).

`sandbox shell` is described under [SSH](#ssh). Commands run with
`sandbox exec`, SSH logins, and `ssh HOST COMMAND` all start in `/workdir`,
or in the directory given with `--workdir`. A relative path in `rsync`,
`scp`, or `sftp` is relative to `/workdir`, except for `scp` and `sftp`
over Tailscale SSH, which start in `/root`.

### Copying changes with `sandbox sync`

`sandbox sync` copies the uncommitted changes of the checkout you run it in
into the same repository's clone in a sandbox, the `/workdir/NAME` that
`sandbox up --repo=OWNER/NAME` makes; `--pull` copies the sandbox's changes
back. NAME comes from the checkout's GitHub `origin`, or else its directory;
`--path=DIR` names another checkout in the sandbox. Without a sandbox, it
means the only one that runs, as for `sandbox shell`.

```sh
cd ~/src/agentix
uv run agentix sandbox up --name=dev --repo=thehumanworks/agentix
# edit here, then
uv run agentix sandbox sync dev
# let an agent work there, then
uv run agentix sandbox sync dev --pull
```

Both sides run one script, `agentix-sync`, through Git: the image has it on
PATH, and the host runs the same file from `nix/` (or `nix run
./nix#agentix-sync`). It needs Git 2.40 or newer on the host.

- What moves: the working tree against HEAD, with untracked files,
  deletions, binary files, modes, and symlinks; not ignored files. It goes
  as a Git bundle without the commits the other side has, so only the
  changes cross.
- It merges: the sender's changes are merged into the receiver's working
  tree as `git merge` would, from the last state both sides agreed on
  (`refs/agentix-sync/synced`), else from the sender's HEAD. So a file
  synced before and changed since on one side is no conflict, and the two
  may be on different commits; then only the uncommitted changes move, and
  the sender's commit is kept in `refs/agentix-sync/incoming^` there.
  Changes show as unstaged, and new files as untracked; the index and HEAD
  stay.
- It refuses conflicts: if a file changed on both sides in ways that do not
  merge, or writing would remove a file that Git ignores there (one where a
  new file or a new file's directory goes, or one inside a directory that a
  file replaces), nothing changes on either side and the files are listed.
- It stops before writing anything if Git cannot read the working tree (a
  failing filter, such as LFS without git-lfs, or an unreadable file), if
  the new files cannot be prepared, or if a file changed while it ran. It
  writes by moving the old files aside and the new ones in, and if a move
  fails it puts the old files back.
- It keeps a backup: before it writes, the receiver's working tree, untracked
  files included, is saved as a commit under
  `refs/agentix-sync/backup/NNNNNN-TIME`, numbered in order. `agentix-sync
  undo` there puts back the latest one, after saving the current state the
  same way. Any failure after the backup was taken prints its ref and the
  undo command. Backups accumulate, with every untracked file they held;
  delete them all with `git for-each-ref --format='delete %(refname)'
  refs/agentix-sync/backup/ | git update-ref --stdin`.
- It refuses changes over 1 GiB, and warns from 100 MiB: untracked files
  that are not ignored are sent too.
- A sandbox from an image without `agentix-sync`, or with an older one, gets
  the host's copy of the script for that run.

## Sandbox lifecycle

### Names

A name has up to 63 lowercase letters, digits, and dashes. It starts with a
letter, not with `sb-`, and ends with a letter or digit, so it is also a
hostname. `sandbox up` without `--name` picks a free name such as
`brisk-otter`. `sandbox up --name=NAME` refuses a name that exists already:
restore it or purge it first.

A name keeps a record: its current sandbox, every sandbox it has had, their
snapshots, and the options it was created with.

```sh
$ uv run agentix sandbox ls
{"name": "dev", "sandbox_id": "sb-...", "status": "stopped", "snapshots": 2}
{"name": "", "sandbox_id": "sb-...", "status": "running", "snapshots": 0}
```

### Snapshots and restore

Modal saves the filesystem of a sandbox when it ends: on `sandbox down`, at
its timeout, or when its main process dies. These exit snapshots are an
experimental Modal feature. `sandbox snapshot` saves a running sandbox on
request. Modal does not report that a sandbox ended by itself, so `ls`,
`snapshots`, `restore`, `purge`, and `shell` check and record the snapshot
when they find one.

```sh
$ uv run agentix sandbox snapshots dev
{"id": "im-...", "created_at": "2026-11-15T09:12:40+00:00", "sandbox_id": "sb-...", "kind": "down", "expires_at": "2026-12-15T09:12:40+00:00", "expired": false}
```

`kind` is `down`, `manual` (from `sandbox snapshot`), `timeout`, or `exit`
for a sandbox that ended another way; an `exit` snapshot is dated when
agentix noticed it, so it can be older than it says. Modal deletes every
snapshot 30 days after it was taken and cannot say whether one still exists,
so `expires_at` and `expired` are computed from the date.

`sandbox restore NAME` starts a new sandbox under the name, from the latest
snapshot or from `--snapshot=ID`, with the options the name was created
with. The current sandbox of the name must have ended. It accepts
`--tailscale` and `--hostname` as `sandbox up` does. The new sandbox has the
saved files, including `/workdir`, `/root`, and `/tmp`, but no processes:
SSH and Tailscale run again once started, for example by `sandbox shell`.
`/run` is not saved, so no snapshot holds the 1Password token; the new
sandbox writes it again. A restored sandbox runs the image its snapshot came
from: a newer published image reaches a name only through `purge` and
`up`.

### Resuming after a timeout

Modal ends a sandbox at its timeout without waiting for the commands in it.
`sandbox exec NAME 'COMMAND' --resume` runs COMMAND so that, if the named
sandbox reaches its timeout first, agentix restores the name from its exit
snapshot and continues COMMAND there, up to 3 times. `--resume='OTHER'`
(or `--resume-with='OTHER'`) names the command that continues it instead.

```sh
uv run agentix sandbox exec dev 'claude -p "Fix the failing tests"' --resume
uv run agentix sandbox exec dev './build.sh' --resume='./build.sh --continue'
uv run agentix sandbox resume dev
```

How a command continues is a rule per harness, in
[`src/agentix/resume.py`](src/agentix/resume.py). `claude -p TASK ...`
starts with its own `--session-id` and continues as
`claude --resume ID -p CONTINUE ...`; `pi -p TASK ...` starts and continues
with its own `--session-id ID`; `codex exec TASK OPTIONS` continues as
`codex exec OPTIONS resume --last CONTINUE`. CONTINUE is a neutral prompt
(carry on, check what is done first), not the task again, so that the task
is not started twice. Still, a resumed agent may repeat a step whose result
the snapshot lacks, such as a pull request opened just before the timeout:
write tasks with side effects so they check first. `codex exec` cannot be
given a session ID, so codex takes the latest session in the workdir: run
one codex run per workdir. A command with no rule, one that names its own
session, and one run through `sh -c` are refused unless `--resume='COMMAND'`
says how it continues. Nothing is resumed that was not started with
`--resume`. Only named sandboxes that keep exit snapshots can resume. See
[docs/resume.md](docs/resume.md).

`exec --resume` prints on standard error what a timeout will lead to, and
at each resume the snapshot, the new sandbox, and the command that
continues. Only that `exec` watches for the timeout: if it is not running
(the laptop slept, the terminal closed), the command still runs until the
timeout, and `sandbox resume NAME` continues it later, while Modal keeps the
snapshot. With nothing to continue, `sandbox resume` is `sandbox restore`.
How it works and how it fails: [docs/resume.md](docs/resume.md).

### Purge, no snapshot, ephemeral

- `sandbox purge NAME` deletes every snapshot of the name, then the name. It
  refuses while the sandbox runs; `--terminate` stops it first and deletes
  the snapshot that leaves.
- `sandbox up --no-snapshot` starts a named sandbox that Modal saves nothing
  of when it ends. `sandbox snapshot` still works, and is then the only way
  to have something to restore.
- `sandbox up --ephemeral` starts a sandbox with only an ID: no name, no
  record, no snapshot. `sandbox ls` lists it while it runs, and
  `sandbox down ID` leaves nothing behind. It refuses `--name`.

### Prune old snapshots

`sandbox prune --older-than=14d` clears out old snapshots of every name in
one go. AGE is a number with a unit: `w`, `d`, `h`, `m`, or `s`, such as
`14d`, `12h`, `30m`, or `1d12h`; anything else is refused. It first lists
what it would delete, with each item's name, kind, ID, and age, and a total:

```sh
$ uv run agentix sandbox prune --older-than=14d
NAME  KIND             ID         AGE     DATE
dev   snapshot (down)  im-...     20d 4h  2026-10-26T09:12:40+00:00
old   snapshot (down)  im-...     31d 0h  2026-10-15T08:00:02+00:00
old   stopped sandbox  sb-...     31d 0h  2026-10-15T08:00:02+00:00
Total: 3 items: 2 snapshots and 1 stopped sandbox, across 2 names; the oldest is 31d 0h old. Modal does not report snapshot sizes.
Delete these? Type yes to delete:
```

It deletes only after you type `yes`, or with `--yes`. Without a terminal
and without `--yes` it only lists. It prunes every kind of snapshot: exit
snapshots (`down`, `timeout`, `exit`) and `manual` ones from `sandbox
snapshot` alike; the KIND column says which. A stopped sandbox is listed
when its name would be left with no snapshot and nothing of it is newer
than AGE; its record then goes as with `purge`. `--keep-latest` keeps the
newest recorded snapshot of each name, and so the name; if that snapshot is
past Modal's 30 days it can no longer be restored from, and the listing
marks such snapshots `expired`. `--name=GLOB` limits it to the names that
match, such as `--name='scratch-*'`.

Prune only acts on what the records hold, so it never deletes an image or
snapshot that agentix did not record. Like `sandbox ls`, the listing asks
Modal about sandboxes of the listed names that have ended and records the
exit snapshots it finds, so even a dry listing can write records; it never
deletes without confirmation. A name whose sandbox runs, or whose exit
snapshot Modal is still saving, is skipped whole and listed as skipped.
Just before deleting, each name is checked again: one that has a new
sandbox since the listing, for example from a restore, or that Modal shows
running, is skipped, and a record is only removed if it holds no sandbox or
snapshot that was not in the listing. Expired snapshots are gone from Modal
already; prune removes them from the record. If anything fails, prune says
which names and exits with an error; run it again.

### Where the records live

Modal cannot list snapshots, so the records are the only way to find and
delete them. They are JSON files on the Modal Volume `agentix-sandboxes`, in
`records/NAME/`, and the Modal Dict `agentix-sandboxes-cache` keeps a copy so
that `sandbox ls` reads no files; losing the cache loses nothing. Writing a
record takes over a second, which `up`, `down`, `restore`, `snapshot`, and
`purge` each spend once. Two commands that change one name at once both keep
their change.

Earlier versions kept the records in the Dict `agentix-sandboxes`. Those are
merged into the Volume when read, and `sandbox purge` removes both. Set
`AGENTIX_STORE=NAME` to use the Volume `NAME` and the Dicts `NAME-cache` and
`NAME` instead, for example to try a change without touching the real
records.

## SSH

There are two SSH engines. Each is a Home Manager module of its own,
`agentix.sandbox.tailscale` and `agentix.sandbox.openssh`, and both are in
the image; with one turned off, starting that engine says so. See
[SSH engines](nix/README.md#ssh-engines).

| Engine | How you reach the sandbox | Needs |
| --- | --- | --- |
| `tailscale` (default) | Tailscale SSH on your tailnet, as `ssh root@NAME` | The 1Password secret, for the auth key; your computer on the tailnet; a tailnet policy that allows SSH |
| `openssh` | sshd behind a Modal TCP tunnel to port 22 | An SSH public key on your computer |

Nothing listens for SSH until an engine starts. Tailscale SSH admits whom
the tailnet policy allows; OpenSSH accepts public keys only.

### `sandbox shell`

```sh
uv run agentix sandbox shell dev
uv run agentix sandbox shell dev -- pwd
uv run agentix sandbox shell dev --engine=openssh --public-key=~/.ssh/other.pub
```

`sandbox shell` starts SSH unless it runs already, waits until the sandbox
answers, and then becomes `ssh`, so the session has your terminal and ends
with the sandbox's exit code. What follows `--` runs in the sandbox, as with
`ssh HOST COMMAND`, with a terminal when yours is one. Without a name, it
opens the only running sandbox, or lists them when several run. It does not
restore a stopped sandbox: it says which snapshot `sandbox restore` would
use. SSH that runs already keeps its Tailscale hostname unless you pass
`--hostname`. For OpenSSH, `--public-key=PATH` also uses the private key
next to it.

It checks the sandbox's identity with what the sandbox reports through
Modal, not by trust on first use: Tailscale reports its host keys, OpenSSH
the fingerprint of its key, and the key the tunnel presents is used only if
it matches. ssh then runs with `StrictHostKeyChecking=yes` and a known_hosts
file of agentix in `~/.local/state/agentix/known_hosts/` (or
`$XDG_STATE_HOME/agentix/known_hosts/`), so it never asks, refuses any other
key, and leaves `~/.ssh/known_hosts` alone. Over Tailscale it connects to
the node's address under its MagicDNS name, so it also reaches a node that
Tailscale named `dev-1`.

A plain `ssh root@dev` uses your own known_hosts file. A restored sandbox
keeps the host keys of its snapshot, but a new sandbox under a purged name
has new ones, which plain ssh refuses until you remove the old entry with
`ssh-keygen -R dev.TAILNET.ts.net`.

### Tailscale

```sh
uv run agentix sandbox ssh start dev
uv run agentix sandbox ssh status dev
uv run agentix sandbox ssh stop dev
```

`start` prints `status`, `online`, `magicdns`, `ipv4`, and `ssh_command`.
`sandbox up --tailscale` and `sandbox restore --tailscale` do the same as part
of starting; if the sandbox starts but Tailscale does not, the error names
the sandbox, which keeps running. The sandbox enrolls with the auth key at
`op://agentix/tailscale/auth/key`, so `--tailscale` with `--secret-name=''`
is refused before anything starts.

The Tailscale hostname is the sandbox name, so `ssh root@dev` reaches the
sandbox `dev`, also after a restore. A sandbox without a name, or with a
name from an earlier version that ends with a dash, gets its sandbox ID in
lowercase. `--hostname=NAME` sets another hostname, also on a node that
already runs.

Tailscale gives each node a unique name. If another node has the name
already, usually an earlier sandbox of the name that ended less than about
an hour ago, the sandbox becomes `dev-1`, and `ssh root@dev` reaches the
other node. `start` then adds a `warning` and prints an `ssh_command` that
reaches the sandbox. With an ephemeral auth key, Tailscale removes an
offline node after about an hour; each `sandbox ssh start dev` after that
renames the sandbox away and back, which takes `dev` once it is free.

Tailscale keeps its state in `/root`, so a restored sandbox rejoins as the
same node with the same address. If the tailnet removed that node
meanwhile, `start` logs the old node key out and enrolls the sandbox again,
under the same name with a new address, in about ten seconds. The host keys
stay either way.

`stop` turns Tailscale SSH off but keeps the node connected.

### OpenSSH

```sh
uv run agentix sandbox ssh start dev --engine=openssh
uv run agentix sandbox ssh status dev --engine=openssh
uv run agentix sandbox ssh stop dev --engine=openssh
```

`start` uses `~/.ssh/id_ed25519.pub`, then `~/.ssh/id_rsa.pub`, or the key
given with `--public-key=PATH`; no private key leaves your computer. It
prints `host`, `port`, `ssh_command`, and `host_key_fingerprint`. Run the
`ssh_command`, adding `-i PRIVATE_KEY` if the key is not a default identity,
and compare the fingerprint with the first connection prompt.

A started listener keeps its key; to change it, stop OpenSSH and start it
with the new key. Stopping closes the listener, and open sessions can
finish. The engines are independent: stopping one leaves the other running.
OpenSSH keeps its host key, state, and log in
`/root/.local/state/agentix/openssh`, so a restored sandbox keeps the
fingerprint.

## Secrets

Sandboxes get the Modal secret `1password` (or the one named with
`--secret-name`), which must hold `OP_SERVICE_ACCOUNT_TOKEN`. One way to
create it, keeping the token off the command line, is from a file that
holds a line `OP_SERVICE_ACCOUNT_TOKEN=...`:

```sh
uv run modal secret create 1password --from-dotenv PATH
```

Image builds and `flake update` get no secrets.

Modal gives the token only to the processes it starts, such as
`sandbox exec`. So that SSH sessions have it too, a sandbox writes it at
start to `/run/agentix/op-token`, which only root can read and which lives
in memory: `/run` is a tmpfs, and no snapshot holds it. The image's `op`
loads that file when `OP_SERVICE_ACCOUNT_TOKEN` is not set; an exported
token takes precedence. To use another token for one SSH session, export it
locally and pass `-o SendEnv=OP_SERVICE_ACCOUNT_TOKEN`: the OpenSSH listener
accepts it, and Tailscale SSH does when the tailnet's SSH rule has
`"acceptEnv": ["OP_SERVICE_ACCOUNT_TOKEN"]`
([Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh#acceptenv)).
The `claude`, `codex`, `pi`, `gh`, `gh-lf`, and `ts` wrappers call
that `op`, and read these items from a 1Password vault named `agentix`:

| Item | Used by |
| --- | --- |
| `op://agentix/claude/oauth/token` | `claude`, which starts Claude Code logged in |
| `op://agentix/codex/auth` | `codex`: Codex's ChatGPT login, `~/.codex/auth.json`, as one line of JSON; see [Codex and pi sign-in](#codex-and-pi-sign-in) |
| `op://agentix/pi/auth` | `pi`: pi's providers, `~/.pi/agent/auth.json`, as one line of JSON |
| `op://agentix/gh/dev/host`, `op://agentix/gh/dev/token` | `gh`, and Git's credential helper for github.com |
| `op://agentix/gh/work/host`, `op://agentix/gh/work/token` | `gh-lf`, the same for a second GitHub host |
| `op://agentix/tailscale/auth/key` | `ts`, when a sandbox enrolls in the tailnet |

A missing item breaks only the command that reads it; `codex --version` and
`pi --version` (and `--help`) skip 1Password. `agentix.codex.secrets` and
`agentix.pi.secrets` can add API keys as environment variables, such as
`ANTHROPIC_API_KEY` for pi; see [nix/README.md](nix/README.md).
Claude, Codex, and pi start without the token in their environment, so the
commands they run do not inherit it, and a permission rule stops Claude
reading the token file. Its commands still run as
root and can run `op`: this prevents accidental exposure and is not a
security boundary. Anyone with a root session in the sandbox can use the
token. A sandbox started with `--secret-name=''` has no token file.

### Codex and pi sign-in

Codex and pi sign in with OAuth logins that they keep in files:
`~/.codex/auth.json` (ChatGPT) and `~/.pi/agent/auth.json` (one entry per
provider). Both refresh their tokens on their own, and a refresh token works
once: when one machine refreshes, the copy on every other machine stops
working. [agent-auth-sync](nix/packages/agent-auth-sync/README.md) keeps
each file in step with its 1Password field on every machine that runs it.
It pushes a file when it changes, and pulls the field when another machine
changed it. When both changed, the newer tokens win.

- In a sandbox, `codex` and `pi` run `agent-auth-sync ensure` before they
  start. If no watcher runs, it starts one in the background, which pulls
  both files and pushes the sandbox's refreshes back. Each agent waits for
  the first pull of its own file, up to 10 seconds: when 1Password does not
  answer, the agent starts anyway (`AGENT_AUTH_SYNC_ENSURE_WAIT` changes
  the wait).
  `agent-auth-sync status` shows whether the files and fields agree. Without
  a token, as with `--secret-name=''`, it does nothing.
- On your Mac (or a Linux computer), run one command from the root of a
  clone of this repository:

  ```sh
  export OP_SERVICE_ACCOUNT_TOKEN=...   # the service account token
  uv run agentix auth install
  ```

  What it needs first:

  - A clone of this repository and [uv](https://docs.astral.sh/uv/); run it
    from the clone with `uv run`, as every agentix command. agentix does not
    need to be installed, and no Modal account or Nix is needed for this.
  - `OP_SERVICE_ACCOUNT_TOKEN` exported in that same shell, for a service
    account with read and write access to the `agentix` vault. `install`
    checks it with `op whoami` and stops if `op` refuses it.
  - `op` (the 1Password CLI) and `jq` on PATH. macOS 15 and later include
    `jq`; otherwise `brew install jq` (and `brew install 1password-cli`).
  - Optionally, `codex login` and pi's `/login` first. If the files are
    missing here, it pulls them from 1Password instead.

  This saves the token in `~/.config/agent-auth-sync/op-token` (mode 600),
  copies the script to `~/.local/bin`, and starts it as a launchd agent on
  macOS or a systemd user service on Linux; afterwards the clone and the
  shell's token are no longer needed. Home Manager users can set
  `agentix.authSync.service.enable = true` instead, then run
  `agent-auth-sync save-token` once. `uv run agentix auth status`,
  `auth sync`, and `auth uninstall` do the rest.

The service account needs write access to the vault for these pushes. A new
login on one machine (`codex login`, pi's `/login`) reaches the others.
Logging out does not stick: a deleted file, or pi's `{}`, is pulled back
from 1Password, and a cleared field is pushed back by any machine that
still has the file. To end a login everywhere, revoke it with the provider.

These modules change Codex and pi on a Mac or Linux computer too:

- Codex keeps its login in `~/.codex/auth.json`
  (`cli_auth_credentials_store = "file"`) rather than the macOS Keychain or
  another keyring; a login only in the keyring is not read, so run
  `codex login` once.
- `codex` and `pi` no longer get `CODEX_API_KEY` or `OPENAI_API_KEY` from
  1Password by default; `agentix.codex.secrets` and `agentix.pi.secrets`
  add them back.

## The committed .env

`.env` is tracked in Git, and this repository is public, so it must never hold
a plaintext secret. Each value is a 1Password reference (`op://VAULT/ITEM/FIELD`)
or a value encrypted by [dotenvx](https://dotenvx.com) (`encrypted:...`).
`dotenvx run -- uv run agentix ...` or `op run --env-file .env -- uv run
agentix ...` resolves the references at run time. Other env files, such as
`.env.local` and `.env.keys`, stay ignored; only `.env` and `*.example` files
may be committed.

The [`Envfile`](Envfile) at the root is dotenvx's policy file
([docs](https://github.com/dotenvx/dotenvx/blob/v2.38.2/README.md)): it
declares each key of `.env`, marks every value `redacted: true` (masked in
dotenvx's output) and `encrypted: true` (never committed as plaintext), and
sets `strict true`, so a declared key that is missing fails.
`scripts/check_env.py` enforces it, for every tracked or untracked env file
that Git does not ignore (`.env*`, `*.env`, `.dev.vars*`, `.flaskenv`, and
files under a `.env` or `.env.d` directory, in any letter case):

- every line is blank, a comment, or `KEY=VALUE` on one line, optionally
  after `export`, with the value optionally quoted. Anything else fails, so
  a pasted secret on a line of its own, or under a key with a space in it,
  cannot slip past a dotenv parser;
- every value, including one in a commented-out `# KEY=...` or `# KEY: ...`
  line, is empty, a single-line `op://VAULT/ITEM/FIELD` or
  `op://VAULT/ITEM/SECTION/FIELD` reference (letters, digits, `.`, `_`, `-`,
  and inner spaces, which need quotes, plus an optional `?attribute=` or
  `?ssh-format=`), or dotenvx ciphertext; `DOTENV_PUBLIC_KEY*` values must
  be public keys;
- the `Envfile` holds only comments, `strict true`, and `env "KEY"` lines
  whose options are `redacted: true`, `encrypted: true`, `optional`,
  `required`, or `type`. `encrypted: false`, `redacted: false` (which would
  print the value in public CI logs), defaults, profiles, and file blocks are
  refused, and so is any other `Envfile` in the tree;
- every key must be declared in the `Envfile` (`DOTENV_PUBLIC_KEY` excepted);
- `dotenvx protect --git-file`, the check behind dotenvx's own Git filter,
  and `dotenvx check` must pass. They run in a scratch copy that holds only
  the root `Envfile` and the file, with 1Password resolution, key stores,
  Armor, and shell values all off, so they need no secret and a shell
  variable cannot stand in for a missing key.

Templates (`*.example`, `*.sample`, `*.template`) skip the declaration
check and `dotenvx check`, so they may leave out keys and values.

Some ordinary-looking forms are refused on purpose, because a secret could
hide in them: spaces around `=` (`KEY = value`), an inline comment after a
value (`KEY=op://v/i/f # note`; put the note on its own line), a trailing
comment on an `Envfile` line, and a comment that starts like an assignment
(`# KEY=...` or `# Note: ...`), whose value must then pass the rule above. The script prints key names,
line numbers, and dotenvx's redacted preview, never a value. It needs
dotenvx 2.32.4 to 2.38.x, the releases that read an `Envfile`, and says
which `dotenvx` on `PATH` it found when that one is outside the range.

With `--staged`, as the hook runs it, it checks the staged versions against
the staged `Envfile`; when the `Envfile` itself is staged, it rechecks every
tracked env file.

Run it with `python3 scripts/check_env.py`. It runs in CI (the `Nix flake
check` job, and the flake's `env-check` check), and as a pre-commit hook on
the staged versions once a clone opts in, which changes only this clone's
`.git/config`:

```sh
git config core.hooksPath .githooks
```

dotenvx comes from the Home Manager profiles (`modules/tools.nix`), so the
Mac and every sandbox have it; elsewhere `nix shell nixpkgs#dotenvx`. `dotenvx
protect`, run once, installs the same check as a Git filter for every
repository on the machine; it changes the global Git configuration, so it is
left to you.

To add a secret:

1. Declare it in `Envfile`: `env "NEW_KEY", redacted: true, encrypted: true`.
2. Either store it in 1Password and add `NEW_KEY=op://agentix/ITEM/FIELD` to
   `.env`, or encrypt it into `.env` with dotenvx, which writes the private
   key to `.env.keys`. `read -rs` keeps the value out of shell history:

   ```sh
   read -rs value && dotenvx set NEW_KEY "$value"; unset value
   ```

   Keep `.env.keys` out of Git (it is ignored) and in 1Password; without it
   no one can decrypt.
3. Run `python3 scripts/check_env.py` before committing.

This is dotenvx 2.38, nixpkgs' release. Its next release replaces `Envfile`
with `Dotenvxfile` and removes `dotenvx check`, so a nixpkgs update that
brings it needs this file, the script, and the hook moved over.

## The image

### How it is built

`uv run agentix image build` publishes the flake's OCI image
(`nix build .#oci` in `nix/`) as a Modal image. Modal's image builder cannot
run that build, so a temporary VM sandbox does, started from the official
`nixos/nix:2.35.6` image pinned by digest (`NIX_BUILDER` in
`src/agentix/image.py`). It builds the OCI image and unpacks its layers into
a directory, and a snapshot of that directory becomes the image's root. The
build adds the environment and working directory from `nix/sandbox.toml`,
runs its checks in a layer it does not publish, and publishes the result as
`determinate-nix:latest`. No base image, registry, or registry credential is
involved, and nothing of the builder reaches the image.

The name `determinate-nix:latest` is left from when the image was Debian
with Determinate Nix installed. It now holds neither: no distribution at
all, and the Nix of the locked nixpkgs (2.35.6 with the current lock). The
end of [OCI image](nix/README.md#oci-image) lists what else changed.

The unpacked root is kept as the Modal image `agentix-rootfs:HASH`, where
the hash covers every file in `nix/`, the builder pin, and the build script.
A build with the same inputs reuses it and takes seconds; a build that
misses starts from an empty Nix store and takes one to two minutes.
`--force-build` builds the root again. If `nix/flake.lock` is missing, the
build creates it first. See [Modal](nix/README.md#modal) for the details.

### What is in it

Root's Home Manager generation, activated at build time, with:

- search and data tools (ripgrep, fd, fzf, ast-grep, jq, yq, sqlite,
  psql), network and archive tools, make and just, hyperfine, watchexec,
  difftastic (`difft`), and nix-output-monitor (`nom`);
- a C and C++ compiler (`cc`, `gcc`, `g++`) with binutils;
- a headless Chromium, `chrome-headless-shell`, which Playwright finds too;
- comma, so `, COMMAND` runs a command that is not installed, with a prebuilt
  nix-index database, and `nix-locate`;
- Python 3 with uv, Bun, and Node; ruff, ty, shellcheck, shfmt, dotenvx, nixfmt,
  statix, and deadnix; language servers for Python, TypeScript, Bash, Nix,
  and more, used by Neovim and by Claude Code's LSP tool;
- Claude Code with its settings and plugins, and `~/.claude/CLAUDE.md`, a
  brief that tells Claude how the sandbox works;
- the Codex CLI (`codex`) and the pi coding agent (`pi`), from nixpkgs,
  signed in from 1Password through `agent-auth-sync`, with the same brief as
  `AGENTS.md`; Codex runs without approval prompts, as Claude does; pi
  starts on `openai/gpt-6-astra`, cycles with Ctrl+P between it and
  `openai/gpt-6.1-sol`, both with a 400000-token context window, and has six
  pi packages installed already (see [nix/README.md](nix/README.md));
- Git with `gh` and `gh-lf`, the 1Password CLI, direnv with nix-direnv,
  Neovim (Nixvim), Rex and herdr, Tailscale, and OpenSSH;
- Nix itself, with `nixpkgs` in the registry pinned to the image's revision,
  and this flake in `/root/.config/home-manager`.

[What a sandbox gets](nix/README.md#what-a-sandbox-gets) has the full list.
The image build checks that every command in the manifest,
`/root/.nix-profile/share/agentix/manifest.json`, resolves in root's
profile.

The image has no `apt` or `dpkg`, no `sudo` (you are root), no `pip` (use
`uv`), and no `perl`, `gpg`, or `useradd`. Run anything missing from nixpkgs,
at the image's revision:

```sh
, hello                       # comma finds the package in its index
nix run nixpkgs#hello
nix shell nixpkgs#gnupg -c gpg --version
```

### Rebuilding

Rebuild after changing anything the image is made from: `nix/` (including
`nix/flake.lock` and `nix/sandbox.toml`) or the build in
`src/agentix/image.py`. [`AGENTS.md`](AGENTS.md) is the rule agents follow:
build, verify in a fresh ephemeral sandbox, remove it. New sandboxes use the
newly published image; running sandboxes and restored names keep theirs.

To try a change in one running sandbox first, run
`home-manager switch --flake /root/.config/home-manager#modal` there. It
changes that sandbox only.

`uv run agentix flake update` moves every input in `nix/flake.lock` except
Rex; `flake update INPUT...` moves only the named ones. It runs Nix in a
sandbox started from the published image, so it needs an image built first,
and writes the lock only once the `modal` profile evaluates with it. With
`--build` it also builds and publishes the image when an input moved.

Rex is pinned by a versioned URL in `nix/flake.nix`, which never changes, so
`nix flake update` leaves it. `uv run agentix flake update rex` points that
URL at the newest snapshot and checks it before writing `flake.nix` and the
lock. See [Rex in sandboxes](nix/README.md#rex-in-sandboxes).

### Profiles and modules

`nix/flake.nix` builds four Home Manager profiles:

| Profile | System | Status |
| --- | --- | --- |
| `modal` | x86_64 Linux | The image; built by `image build` and CI |
| `modal-arm` | ARM64 Linux | Built once under emulation; CI's ARM job should build it, not yet seen green |
| `linux` | x86_64 Linux | An example of `lib.mkLinuxHome` for a user `agent`; built by CI |
| `mac` | Apple Silicon macOS | Evaluated by CI, not built; see [macOS later](nix/README.md#macos-later) |

For any other Linux machine or container, `lib.mkLinuxHome { username;
homeDirectory; }` gives a configuration for that user, root or not, without
Modal. The sandbox modules that rely on what `sandbox up` writes under
`/run/agentix`, or on root, stay off there. See
[Any Linux user](nix/README.md#any-linux-user).

Every module has an `enable` option: `agentix.NAME.enable` for the shared
modules, which default to on, and `agentix.sandbox.NAME.enable` for the
sandbox modules, which follow `agentix.sandbox.enable`, set by the sandbox
profile. The flake exports them as `homeManagerModules`
(`default` imports all) for use from other flakes, and builds the OCI image
for Docker and Podman too. The Git identity comes from a user file in
`nix/home-manager/users/`; use your own before building for yourself. See
[nix/README.md](nix/README.md) for the modules, the manifest, and the OCI
image.

## Repository layout

```text
.
├── AGENTS.md               rules for agents: when and how to rebuild and verify the image
├── .env                    tracked: op:// references and ciphertext only
├── Envfile                 the keys .env may hold; see "The committed .env"
├── scripts/check_env.py    fails on plaintext in env files; .githooks/pre-commit runs it
├── pyproject.toml          the agentix package (click, modal), Python 3.14
├── src/agentix/
│   ├── __main__.py         the command line: one function per command, and its flags
│   ├── cli.py              the click layer: old Fire spellings, help, printing results
│   ├── local.py            runs each operation with one Modal session
│   ├── lifecycle.py        up, down, ls, snapshot(s), restore, purge
│   ├── resume.py           exec --resume and sandbox resume, with the resume rules
│   ├── runs.py             exec --detach, sandbox logs, wait, and runs
│   ├── runner.py           the deployed Modal function that runs detached commands
│   ├── store.py            the record of a name and how copies merge
│   ├── recordfiles.py      records as files on the Volume, cached in a Dict
│   ├── sandbox.py          Modal sandbox calls and the sandbox start command
│   ├── session.py          shared Modal handles and error mapping
│   ├── ssh.py              SSH engines, sandbox shell, host keys
│   ├── ssh_input.py        checks on SSH flags and keys, made before Modal
│   ├── sync.py             sandbox sync: runs agentix-sync on both sides
│   ├── auth.py             auth install/uninstall/status/sync: runs agent-auth-sync here
│   ├── names.py            sandbox name rules and generated names
│   ├── image.py            the image build, its checks, and NIX_BUILDER
│   ├── flake.py            flake update and the Rex pin
│   ├── layout.py           paths and environment from nix/sandbox.toml
│   ├── defaults.py         default image, secret, timeout, memory, store
│   ├── config.py           defaults for sandbox up from ~/.config/agentix/config.toml
│   └── scripts/            tailscale-ssh.sh and openssh-ssh.sh, run in the sandbox
├── nix/                    the flake; see nix/README.md
│   ├── flake.nix           inputs, profiles, exported modules, OCI, agentix-sync, and agent-auth-sync packages, checks
│   ├── sandbox.toml        paths and environment shared by the CLI and the image
│   ├── home-manager/       profiles/, users/, modules/, home.nix, exports.nix
│   ├── packages/           agentix-sync, and agent-auth-sync, which also runs without Nix
│   └── oci/                the OCI image the Modal image is made from
├── docs/                   design notes, such as resume.md
├── tests/                  unit tests; Modal is faked, nothing reaches it
└── .github/                CI, the weekly lock update, Dependabot
```

## Development

```sh
uv run --with pytest pytest -q
uv run ruff check
uv run ruff format --check
python3 scripts/check_env.py
```

The env check's tests need dotenvx and are skipped without it.

The tests never contact Modal. `.python-version` selects the standard
CPython 3.14 build; Modal imports take about twice as long on the
free-threaded one.

The Nix checks need Nix, so run them in a sandbox, which has it, from a copy
of `nix/` (or from `/root/.config/home-manager` to check the published
image's copy):

```sh
nix fmt
nix flake check
nix flake check --all-systems --no-build
```

See [Checks and formatting](nix/README.md#checks-and-formatting). CI runs
the same checks.

## CI

GitHub Actions run two workflows from `.github/workflows/`. Neither needs a
secret or contacts Modal.

- `ci.yml` runs on pull requests, pushes to `main`, and on demand, with three
  jobs that take about a minute. `Python tests and lint` runs the tests,
  `ruff check`, and `ruff format --check`. `Nix flake check` runs
  `scripts/check_env.py` with the pinned dotenvx (see
  [The committed .env](#the-committed-env)), then
  `nix flake check --all-systems --no-build`, which evaluates the x86_64,
  ARM, and Mac outputs, then `nix flake check` on x86_64 Linux: formatting,
  statix, deadnix, the `modal` and `linux` profiles, the OCI image's root
  and layer list, each SSH engine module on and off, that the harness
  wrappers keep the 1Password token from the harness, and `env-check`, which
  runs the env check and its tests. `Nix flake check
  (ARM)` runs the same `nix flake check` beside it on an `ubuntu-24.04-arm`
  runner, which builds `modal-arm`, the ARM image's root and layer list, and
  the SSH engine checks. That job has not yet been seen green on GitHub. To
  require CI before merging, require those three checks by name.
- `flake-update.yml` runs every Monday and on demand. It runs
  `nix flake update` in `nix/` and the same Nix checks on the new lock,
  building x86_64 only; ARM is built when CI runs on the pull request. When
  they pass, it opens or updates a pull request from
  `automation/flake-update`. It does not move Rex, and it does not build the
  image: after merging, rebuild and verify as `AGENTS.md` requires. GitHub
  lets it open the pull request only when "Allow GitHub Actions to create
  and approve pull requests" is on in the repository's Actions settings, and
  CI does not start on that pull request until it is closed and reopened.

The runners install the Nix release that `NIX_BUILDER` pins, and keep no Nix
store cache; `.github/actions/nix/action.yml` says why. Each third-party
action is pinned to a commit SHA, and Dependabot (`.github/dependabot.yml`)
proposes newer pins every Monday in one pull request against `main`, for
releases at least seven days old. Pull requests from forks get a read-only
token and no secrets; neither workflow uses `pull_request_target`.

## Status and known limitations

- Errors print as Python tracebacks; the message is the last line.
- A name purged and taken again within about an hour can come back as
  `NAME-1` on the tailnet; see [Tailscale](#tailscale).
- Exit snapshots are an experimental Modal feature, and every snapshot
  expires after 30 days. Nothing restarts SSH or Tailscale inside a sandbox
  on its own; `sandbox shell` and `sandbox ssh start` start them.
- Every image build that misses the cache starts from an empty Nix store;
  there is no binary cache of agentix's own.
- Only the x86_64 image runs on Modal: `image build` builds no other.
  `modal-arm` has been built under qemu emulation. CI's ARM job is expected
  to build it and the ARM OCI image's root from its first run, but has not
  yet been seen green, and nothing runs the ARM image; see [ARM Linux](nix/README.md#arm-linux). CI only
  evaluates `mac`. On an Apple Silicon Mac, Docker runs the x86_64 OCI image
  under emulation; see [OCI image](nix/README.md#oci-image).
- The SSH scripts use commands from other modules: `jq` (tools) for both
  engines, `flock` (base commands) for OpenSSH, and `ssh-keygen` (OpenSSH)
  for the Tailscale host keys that `sandbox shell` checks. With the OpenSSH
  module off, `sandbox ssh start` still works over Tailscale, but
  `sandbox shell` finds no host key and refuses.
- GitHub reads scheduled workflows and the Dependabot configuration only
  from the default branch. While the repository's default branch is not
  `main` and lacks `.github/`, the weekly lock update and Dependabot do not
  run.
- agent-auth-sync reads 1Password every 2 minutes. Two machines that
  refresh the same login within that window still collide, and one of them
  must sign in again; two pushes at the same moment are merged when either
  machine reads the other's back, and otherwise the later one wins; pi's short-lived tokens make this likelier with
  several sandboxes. A sandbox that refreshes and ends before its watcher
  pushes takes the only valid token with it. Its launchd and systemd paths
  are tested with stand-ins only, not on a real Mac or systemd, and not
  under bash 3.2.
- The profiles carry the owner's Git identity and GitHub hosts
  (`nix/home-manager/users/`, `nix/home-manager/modules/git.nix`). Change
  them before building an image for someone else.
