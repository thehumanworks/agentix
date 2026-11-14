# Agentix

Build a Modal image with the Home Manager configuration in `nix/`, then start
development sandboxes from the published image. Run these commands from this
checkout with uv and an authenticated Modal profile.

```sh
uv run agentix image build
uv run agentix sandbox up
```

The build installs Nix and activates the `modal` Home Manager profile. If
`nix/flake.lock` is missing, a temporary sandbox generates it and copies it back
to this checkout. Keep that lockfile in version control. No local Nix install is
needed. The completed image is published as `determinate-nix:latest`; starting
a sandbox does not rebuild it.

New sandboxes receive the Modal secret `1password`, which must contain
`OP_SERVICE_ACCOUNT_TOKEN`. Build containers receive no secrets. Use
`--secret-name=NAME` to select another secret, or `--secret-name=''` to omit it.
Sandboxes default to a one-hour lifetime; change it with `--timeout=SECONDS`.

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

Both engines return an `ssh_command` with `SendEnv=OP_SERVICE_ACCOUNT_TOKEN`.
If this variable is exported in your local shell, SSH sends it over the encrypted
connection. OpenSSH accepts this variable explicitly. Restart an existing
OpenSSH listener with `ssh stop` followed by `ssh start` to apply the setting.
No image rebuild is required.

Tailscale SSH uses the tailnet policy instead of `sshd_config`. Add this field
to the applicable existing SSH rule before using token forwarding with Tailscale:

```json
"acceptEnv": ["OP_SERVICE_ACCOUNT_TOKEN"]
```

This is client-to-server forwarding. It does not make the token injected by
Modal available automatically in SSH sessions. Token values are not written
to the server configuration or included in the printed command.
See the [OpenSSH client documentation](https://man.openbsd.org/ssh_config#SendEnv)
and [Tailscale SSH policy documentation](https://tailscale.com/kb/1193/tailscale-ssh#acceptenv).

Rebuild after editing the Nix configuration. Use `image build --force-build`
only when an uncached build is needed. Modal's image-layer cache is used now;
a separate Nix binary-cache service is deferred. Tailscale startup and persistent
volume mounting remain manual; see [the Nix setup notes](nix/README.md).
