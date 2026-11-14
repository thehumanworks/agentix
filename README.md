# Agentix

Build a Modal image with the Home Manager configuration in `nix/`, then start
development sandboxes from the published image. Run these commands from this
checkout with uv and an authenticated Modal profile.

```sh
uv run agentix image build
uv run agentix sandbox up
```

The build installs a pinned Nix release, activates the `modal` Home Manager
profile, and removes the build inputs from the Nix store. If `nix/flake.lock`
is missing, a temporary sandbox generates it and copies it back to this
checkout. Keep that lockfile in version control. No local Nix install is
needed. The completed image is published as `determinate-nix:latest`; starting
a sandbox does not rebuild it.

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
outside those two.

`sandbox up` prints the new sandbox ID and a name, such as `brisk-otter`.
Choose the name with `--name=NAME`: up to 63 lowercase letters, digits, and
dashes, starting with a letter but not with `sb-`. Every command that takes a
sandbox ID also takes the name:

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
`ipv4`. The default Tailscale hostname is the full sandbox ID in lowercase,
so concurrent and replacement sandboxes have distinct SSH hostnames. You can
also pass the sandbox ID as `--sandbox-id=sb-REPLACE` and override the name with
`--hostname=agentix-dev`. Each start applies the requested name (or the sandbox
ID when omitted), including when Tailscale is already running. Use distinct
custom names for distinct sandboxes.

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

`sandbox restore` starts a new sandbox under the same name, from the latest
snapshot or from `--snapshot=ID`, with the options the name was created with.
It accepts `--tailscale` and `--hostname` as `sandbox up` does. The current
sandbox of the name must have ended. The new sandbox has the saved files,
including `/workdir`, `/root`, and `/tmp`, but no running processes, because
memory is not saved. `/run` is not saved either, so no snapshot holds the
1Password token; the new sandbox stores it again. Tailscale keeps its state in
`/root`, so a restored sandbox rejoins the tailnet as the same node. Its
hostname becomes the new sandbox ID unless you pass `--hostname`.

`sandbox purge NAME` deletes every snapshot of a name, then the name. It
refuses while the sandbox runs; `--terminate` stops it first and also deletes
the snapshot that this leaves. `sandbox up --name=NAME` refuses a name that
exists already: restore it, or purge it first.

The names and their snapshot IDs are kept in the Modal Dict
`agentix-sandboxes`. That is the only record of the snapshots, because Modal
cannot list them. Modal deletes a Dict entry that nothing reads or writes for
7 days, and the snapshots of that name can then no longer be found or purged,
so purge names you no longer need. `sandbox snapshot` keeps a snapshot for
Modal's default of 30 days; Modal does not document how long it keeps the
snapshots it saves when a sandbox ends. These exit snapshots are an
experimental Modal feature.

`.python-version` selects the standard CPython 3.14 build. Modal imports take
about twice as long on the free-threaded build.

For OpenSSH through a Modal TCP tunnel:

```sh
uv run agentix sandbox ssh start sb-REPLACE --engine=openssh
uv run agentix sandbox ssh status sb-REPLACE --engine=openssh
uv run agentix sandbox ssh stop sb-REPLACE --engine=openssh
```

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
also builds the image when one moved. Use `image build --force-build`
only when an uncached build is needed. Modal's image-layer cache is used now;
a separate Nix binary-cache service is deferred. Persistent volume mounting
remains manual; see [the Nix setup notes](nix/README.md).
