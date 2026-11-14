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

Use the returned sandbox ID:

```sh
uv run agentix sandbox exec sb-REPLACE 'nvim --version'
uv run agentix sandbox ssh start sb-REPLACE
uv run agentix sandbox ssh status sb-REPLACE
uv run agentix sandbox ssh stop sb-REPLACE
uv run agentix sandbox down sb-REPLACE
```

`sandbox ssh start` defaults to `--engine=tailscale` and prints `magicdns` and
`ipv4`. You can also pass the sandbox ID as `--sandbox-id=sb-REPLACE` and choose
an initial name with `--hostname=agentix-dev`. If SSH is already running, start
returns the existing address without changing the name or restarting it.
`sandbox ssh stop` disables SSH but keeps Tailscale connected and retains its
identity. Starting again re-enables SSH. These commands run from your computer;
no image rebuild is needed for an image that already includes the `ts` wrapper.

Connect from your own tailnet machine with `ssh root@HOSTNAME`, then run
`herdr` or `nvim`. Your tailnet policy
must permit SSH access. For shell operators, explicitly run a shell, for example
`sandbox exec sb-REPLACE 'bash -lc "command -v nvim; command -v uv"'`.

`sandbox up` returns when the sandbox runs and is ready for commands, which
takes about a second longer than Modal needs to accept it.

## Server

Each command spends most of its time connecting to Modal. `agentix serve` keeps
those connections open, so each command takes about 0.5 seconds instead of 1.5
to 3 seconds:

```sh
uv run agentix serve
```

The server listens on `~/.agentix/server.sock`, a Unix socket that only your
user can open. While it runs, every `agentix sandbox` command uses it
automatically. Without it, commands work in the same way as before. `image
build` always runs in the command itself.

Use `serve --socket=PATH` to listen on another socket, and set
`AGENTIX_SERVER=PATH` for the commands that must use it. A server that you name
in this way must answer. Set `AGENTIX_SERVER=off` to use no server.

The server is local and has no network listener. It is not the public REST or
MCP interface. The operations are in `src/agentix/sandbox.py` and
`src/agentix/ssh.py`, and they share the warm state in `src/agentix/session.py`.
Build a later interface on those modules to get the same speed.

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

Rebuild after editing the Nix configuration. Use `image build --force-build`
only when an uncached build is needed. Modal's image-layer cache is used now;
a separate Nix binary-cache service is deferred. Tailscale startup and persistent
volume mounting remain manual; see [the Nix setup notes](nix/README.md).
