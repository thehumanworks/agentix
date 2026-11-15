# Agentix

Build a Modal image with the Home Manager configuration in `nix/`, then start
development sandboxes from the published image. Run these commands from this
checkout with uv and an authenticated Modal profile.

```sh
uv run agentix image build
uv run agentix sandbox up
```

The image is the flake's OCI image, `nix build .#oci` in `nix/`, so the
same files run on Modal, Docker, and Podman. Modal's image builder cannot run
that build, so a temporary VM sandbox does: it starts from the official Nix
image of a pinned release, builds the OCI image, and unpacks it into a
directory. A snapshot of that directory becomes the image, with the
environment and working directory that `nix/sandbox.toml` gives the OCI
image. No base image, registry, or registry credential is involved, and
nothing of the builder reaches the image. If `nix/flake.lock` is missing, a
temporary sandbox generates it and copies it back to this checkout. Keep that
lockfile in version control. No local Nix install is needed. The completed
image is published as `determinate-nix:latest`, a name kept from when it held
Determinate Nix; starting a sandbox does not rebuild it.

A build from an empty Nix store takes about two minutes. The unpacked files
are kept as the Modal image `agentix-rootfs`, tagged with a hash of `nix/`,
the builder, and the build recipe, so a later build with the same inputs
reuses them and finishes in seconds. `--force-build` builds them again. See
[the Nix setup notes](nix/README.md#modal) for how the build works and how
the image differs from the Debian-based one it replaces.

The image gives an agent search and data tools, Python with uv, Bun and Node,
linters and formatters, language servers, and direnv. A tool that is not
installed runs with `nix run nixpkgs#NAME`, from the same nixpkgs revision as
the image. Claude starts with a brief that describes the sandbox. See
[the Nix setup notes](nix/README.md) for the full list and the layout.

New sandboxes receive the Modal secret `1password`, which must contain
`OP_SERVICE_ACCOUNT_TOKEN`. Build containers receive no secrets. Use
`--secret-name=NAME` to select another secret, or `--secret-name=''` to omit it.
Sandboxes default to a one-hour lifetime; change it with `--timeout=SECONDS`.
A sandbox reserves 8192 MiB of memory and Modal's default CPU share. Change
them with `--memory=MIB` and `--cpu=CORES`.

The image creates `/workdir` and sets it as its working directory, so commands
run with `sandbox exec`, SSH logins, and `ssh HOST COMMAND` start there. A
relative path in `rsync`, `scp`, or `sftp` is therefore relative to `/workdir`.
Tailscale SSH is the exception for `scp` and `sftp`, which start in `/root`.

Claude already trusts `/workdir` and `/root`. Use `--workdir=PATH` to start a
sandbox in another absolute path; Claude asks before trusting a directory
outside those two. The working directory and the files a sandbox writes in
`/run/agentix` are set once, in `nix/sandbox.toml`, which this command and the
image both read.

`sandbox up` prints the new sandbox ID and a name, such as `brisk-otter`.
Choose the name with `--name=NAME`: up to 63 lowercase letters, digits, and
dashes, starting with a letter but not with `sb-`, and ending with a letter or
digit, so that the name is also a hostname. Every command that takes a sandbox
ID also takes the name:

```sh
uv run agentix sandbox up --name=dev
uv run agentix sandbox exec dev 'nvim --version'
uv run agentix sandbox ssh start dev
uv run agentix sandbox ssh status dev
uv run agentix sandbox ssh stop dev
uv run agentix sandbox down dev
```

`sandbox up --tailscale` also starts Tailscale SSH, as `sandbox ssh start`
does, and prints its address next to the sandbox ID. `--hostname=NAME` sets
the Tailscale hostname. A new sandbox enrolls with a key from 1Password, so
`--tailscale` is refused with `--secret-name=''` before anything starts. If the
sandbox starts but Tailscale does not, the error names the sandbox, which keeps
running; retry with `sandbox ssh start`.

`sandbox ssh start` defaults to `--engine=tailscale` and prints `magicdns` and
`ipv4`. The default Tailscale hostname is the sandbox name, so `ssh root@dev`
reaches the sandbox `dev`, also after `sandbox restore dev`. A sandbox without
a name, such as one started with `--ephemeral`, gets its sandbox ID in
lowercase. So does a name from an earlier version that ends with a dash, which
Tailscale would drop. You can also pass the sandbox ID as
`--sandbox-id=sb-REPLACE` and override the name with `--hostname=agentix-dev`.
Each start applies the requested name (or the default when omitted), including
when Tailscale is already running. Use distinct custom names for distinct
sandboxes.

Tailscale gives every node a name of its own. When another node of the tailnet
has the name already, the sandbox becomes `dev-1`, and `ssh root@dev` reaches
that other node. `sandbox ssh start` then adds a `warning` and prints the
`ssh_command` that reaches the sandbox. The other node is usually an earlier
sandbox of the name: one that ended less than an hour ago, before a
`sandbox purge` and a new `sandbox up --name=dev`. With an ephemeral auth key,
Tailscale removes a node about an hour after it went offline (its
documentation says 30 to 60 minutes; two that were watched took 66 and 70). The
sandbox keeps `dev-1` after that, until its hostname changes, so each
`sandbox ssh start dev` renames it away and back, which takes `dev` once it
is free.

For an existing sandbox named `modal`, rerun `sandbox ssh start SANDBOX_ID`
and use the new returned `ssh_command`. This renames its existing Tailscale
node without replacing its identity or host keys. The old `modal` entry in
your SSH known-hosts file can remain; the new name avoids that collision and
keeps SSH host-key checking enabled.

`sandbox ssh stop` disables SSH but keeps Tailscale connected and retains its
identity. Starting again re-enables SSH. These commands run from your computer;
no image rebuild is needed for an image that already includes the `ts` wrapper.

Connect from your own tailnet machine with `ssh root@HOSTNAME`, then run
`herdr` or `nvim`. Your tailnet policy
must permit SSH access. For shell operators, explicitly run a shell, for example
`sandbox exec sb-REPLACE 'bash -lc "command -v nvim; command -v uv"'`.

`sandbox up` returns when the sandbox runs and is ready for commands, which
takes about a second longer than Modal needs to accept it.

`.python-version` selects the standard CPython 3.14 build. Modal imports take
about twice as long on the free-threaded build.

For OpenSSH through a Modal TCP tunnel:

```sh
uv run agentix sandbox ssh start sb-REPLACE --engine=openssh
uv run agentix sandbox ssh status sb-REPLACE --engine=openssh
uv run agentix sandbox ssh stop sb-REPLACE --engine=openssh
```

The image has OpenSSH while the Home Manager module `agentix.sandbox.openssh`
is on, as it is by default; each engine has a module of its own, so either
can be turned off. See [SSH engines](nix/README.md#ssh-engines).

New sandboxes reserve TCP port 22 for this tunnel. Sandboxes created before
this change need to be replaced to use OpenSSH; Tailscale still works on them.
There is no SSH listener until you start one. SSH encrypts the connection and
accepts public-key authentication only.

OpenSSH uses your local `~/.ssh/id_ed25519.pub`, then `~/.ssh/id_rsa.pub` as a
fallback. Select a different public key with `--public-key=PATH`. No private key
is uploaded. Run the returned `ssh_command`; add `-i PATH_TO_PRIVATE_KEY` if
your key is not a default SSH identity. Check the returned host-key fingerprint
against the first connection prompt.

Repeated starts reuse the listener and existing authorized key. To change keys,
stop OpenSSH and start it with the new public key. Stop closes the listener;
already connected OpenSSH sessions can finish. Each engine is managed separately,
so stopping one does not stop the other. OpenSSH state and logs are stored in
`/root/.local/state/agentix/openssh` inside the sandbox.

1Password works in every session of a sandbox without extra flags. Modal gives
`OP_SERVICE_ACCOUNT_TOKEN` only to the processes it starts, such as
`sandbox exec` and `modal shell sb-REPLACE`. SSH sessions do not inherit it, so
each sandbox stores the token in `/run/agentix/op-token` when it starts. Only root
can read that file, and it is held in memory, not on disk. The `op` command in the image
loads that file when the variable is not set, and `claude`, `gh`, `gh-lf`, and
`ts` call the same `op`. Run the returned `ssh_command` as it is.

Claude starts without the token in its environment, so the commands it runs
do not inherit it, and it is denied reading the token file with its Read tool.
Its commands still run as root and can run `op`, so treat this as protection
against accidental exposure, not as a boundary.

Anyone who can open a root session in the sandbox can use the token. Sandboxes
created before this change, and sandboxes started with `--secret-name=''`, have
no token file. A container started directly from the image, for example with
`modal shell --image`, has none either; pass `--secret 1password` there.

An exported `OP_SERVICE_ACCOUNT_TOKEN` takes precedence over the file. To use a
different token for one OpenSSH session, export it locally and add
`-o SendEnv=OP_SERVICE_ACCOUNT_TOKEN`; the listener accepts that variable.
Tailscale SSH needs `"acceptEnv": ["OP_SERVICE_ACCOUNT_TOKEN"]` in the tailnet
SSH rule for the same override. See the
[OpenSSH client documentation](https://man.openbsd.org/ssh_config#SendEnv) and
[Tailscale SSH policy documentation](https://tailscale.com/kb/1193/tailscale-ssh#acceptenv).

Rebuild after editing the Nix configuration. A rebuild keeps the revisions in
`nix/flake.lock`; `uv run agentix flake update [INPUT...]` moves them first, and with `--build`
also builds the image when one moved. Rex moves only when named, as
`flake update rex`; see [Rex in sandboxes](nix/README.md#rex-in-sandboxes). Use `image build --force-build`
only when an uncached build is needed. Each build that misses the cache starts
from an empty Nix store; a Nix binary-cache service is deferred. Persistent
volume mounting remains manual; see [the Nix setup notes](nix/README.md).

## Snapshots

Modal saves the filesystem of a sandbox when it ends: when `sandbox down`
stops it, when it reaches its timeout, or when its main process dies. The name
keeps these snapshots, so a later sandbox can continue where one stopped:

```sh
uv run agentix sandbox ls
uv run agentix sandbox snapshots dev
uv run agentix sandbox restore dev
uv run agentix sandbox restore dev --snapshot=im-REPLACE
uv run agentix sandbox snapshot dev
uv run agentix sandbox purge dev
```

`sandbox ls` lists every name with its current sandbox, whether that sandbox
runs, and how many snapshots it has, followed by running sandboxes that have
no name. `sandbox snapshots` lists the snapshots of a name, oldest first. Each
has a `kind`: `down` and `manual` (from `sandbox snapshot`, which saves a
running sandbox) are dated when they were taken, `timeout` at the timeout, and
`exit`, for a sandbox that ended another way, when agentix first noticed.
Modal does not report that a sandbox ended by itself, so `ls`, `snapshots`,
`restore`, and `purge` check for that and record the snapshot.

Modal deletes every snapshot 30 days after it was taken: those it saves when
a sandbox ends, and those of `sandbox snapshot`. Modal cannot say whether a
snapshot still exists, so `sandbox snapshots` gives each one `expires_at`,
30 days after its date, and `expired` once that has passed. For kind `exit`
the real date can be earlier. `sandbox restore` says when the snapshot it
needs is gone.

`sandbox restore` starts a new sandbox under the same name, from the latest
snapshot or from `--snapshot=ID`, with the options the name was created with.
It accepts `--tailscale` and `--hostname` as `sandbox up` does. The current
sandbox of the name must have ended. The new sandbox has the saved files,
including `/workdir`, `/root`, and `/tmp`, but no running processes, because
memory is not saved. `/run` is not saved either, so no snapshot holds the
1Password token; the new sandbox stores it again. SSH does not run until you
start it again. Tailscale keeps its state in `/root`, so a restored sandbox
rejoins the tailnet as the same node, with the same address, under its name.
If the tailnet has removed that node meanwhile, as it does with an ephemeral
auth key, `tailscaled` still reports `Running` from the saved state, but the
node stays offline and Tailscale asks it to log in. `sandbox ssh start`
notices that, logs the old node key out, and enrolls the sandbox again with
the 1Password key: under the same name, with a new IP address, in about 10
seconds. Either way the Tailscale SSH host keys stay, because they are in the
snapshot too. OpenSSH keeps its host key as well, so its fingerprint stays the
same.

`sandbox purge NAME` deletes every snapshot of a name, then the name. It
refuses while the sandbox runs; `--terminate` stops it first and also deletes
the snapshot that this leaves. `sandbox up --name=NAME` refuses a name that
exists already: restore it, or purge it first.

`sandbox up --no-snapshot` starts a sandbox that Modal saves nothing of when
it ends. It has a name and a record as usual, and `sandbox snapshot` still
saves it on request; without such a snapshot, `sandbox restore` explains
that there is nothing to restore. `sandbox up --ephemeral` keeps no record
either: the sandbox has only its ID, `sandbox ls` lists it among the
sandboxes without a name, and `sandbox down ID` leaves nothing behind. It
refuses `--name`, because a name is found through its record.

The names and their snapshot IDs are kept on the Modal Volume
`agentix-sandboxes`, in `records/NAME/`. That is the only record of the
snapshots, because Modal cannot list them. A Volume keeps its files until
they are deleted; a Modal Dict, which agentix used before, drops an entry
that nothing reads or writes for 7 days. The Dict `agentix-sandboxes-cache`
keeps a copy of the records, so that `sandbox ls` reads no files; losing it
loses nothing. Writing a record takes about 1.5 seconds, which `up`, `down`,
`restore`, `snapshot`, and `purge` each spend once.

Two commands that change one name at once both keep their change. Each write
adds a file and deletes only the files it read, and the next read merges
them. A purge leaves a small file, so that a command that read the name
earlier cannot bring it back.

Records that earlier versions kept in the Dict `agentix-sandboxes` are copied
to the Volume when `sandbox ls` or a command on that name reads them, and
again whenever an earlier version adds to them. The Dict entry stays, so an
earlier version still finds the name, until `sandbox purge` removes both. Set
`AGENTIX_STORE=NAME` to use the Volume `NAME`, the Dict `NAME-cache`, and the
earlier records in the Dict `NAME` instead, for example to try a change
without touching the real records.

These exit snapshots are an experimental Modal feature.

## CI

GitHub Actions run two workflows from `.github/workflows/`. Neither needs a
secret, and neither contacts Modal.

`ci.yml` runs on every pull request and every push to `main`. One job runs
the Python tests, `ruff check`, and `ruff format --check`. The other runs, in
`nix/`, `nix flake check --all-systems --no-build`, which evaluates the
x86_64, ARM, and Mac outputs, and then `nix flake check`, which checks the
formatting, runs statix and deadnix, and builds the `modal` profile and the
OCI image check. On a fresh runner that fetches about 620 MiB and adds about
3 GiB to the store.

`flake-update.yml` runs every Monday and on demand. It runs `nix flake update`
in `nix/` and the same Nix checks on the new lock. When they pass, it opens a
pull request from `automation/flake-update` with the new `flake.lock`, or
updates the one already open. When they fail, the run fails and its summary
lists the inputs that moved. Rex stays at the snapshot that `nix/flake.nix`
names; move it with `uv run agentix flake update rex`. It does not build or
publish the image: after merging its pull request, rebuild the image and
verify it as `AGENTS.md` requires.

The runners install the Nix release whose official image
`src/agentix/image.py` pins as the image's builder, with
`cachix/install-nix-action`. Neither workflow keeps a Nix store cache. Nearly
every path the checks need is on cache.nixos.org, and fetching it from there
was no slower than restoring it from the Actions cache. With Magic Nix Cache
the Nix job took 8.5 minutes, 7 of them spent uploading paths that
cache.nixos.org already serves. On the first run, with nothing to restore, the
two checks took about a minute. `setup-uv` still keeps its own downloads in the
Actions cache.

The weekly job opens its pull request with the workflow's own token. GitHub
allows that only when "Allow GitHub Actions to create and approve pull
requests" is on in the repository's Actions settings. CI does not start on a
pull request opened with that token; close and reopen it to run CI.

Each third-party action is pinned to a full commit SHA, with its version in a
comment. Dependabot (`.github/dependabot.yml`) proposes newer pins every
Monday in one pull request against `main`, for releases at least 7 days old.
GitHub runs scheduled workflows and reads the Dependabot configuration only
from the default branch, so both weekly jobs need their files on it.

The repository is public, so pull requests from forks run `ci.yml`. Their
runs get a read-only `GITHUB_TOKEN` and no secrets, a first-time
contributor's run waits for approval, and neither workflow uses
`pull_request_target`. To require CI before merging into `main`, require the
checks `Python tests and lint` and `Nix flake check`, the jobs' `name:`
values. Renaming a job renames its check. The weekly lock pull request then
needs the close and reopen above before it can merge.
