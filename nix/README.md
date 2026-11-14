# Portable Home Manager

## Tailscale in Linux sandboxes

After Home Manager activation, run as root in the sandbox:

```sh
ts up agentix-dev
```

This starts a userspace daemon without systemd or a TUN device, enables
Tailscale SSH, and prints the MagicDNS hostname and IPv4 address. Enrollment
reads `op://agentix/tailscale/auth/key` through the authenticated 1Password CLI.
You can instead supply `TAILSCALE_AUTHKEY` as an injected key or secret reference.
The key is passed through a file descriptor, not command arguments or a disk file.

Repeated starts reuse the daemon and identity. The daemon outlives the command
and terminal, but is not automatically restarted after a crash or sandbox reboot.
Run the same command from your sandbox startup process to restore it.
Existing authenticated state does not require 1Password to be available.

State defaults to `$HOME/.local/state/agentix/tailscale`. To retain the identity
across replacement sandboxes, mount a persistent volume and use the same path
on each start, for example:

```sh
export TAILSCALE_STATE_DIR=/volume/tailscale/agentix-dev
ts up agentix-dev
```

Use one state directory per machine; do not run cloned identities concurrently.
Create state at runtime, not during the image build. Use a non-ephemeral auth key
if the machine must retain its registration while offline. New sandboxes with
separate identities need a reusable key or individually issued keys.

From another tailnet machine, connect with `ssh root@<printed-hostname>`.
Your tailnet network and SSH policies must permit that access, and MagicDNS
must be enabled for name-based connections. From inside this userspace sandbox:

```sh
ts ssh tomas@other-machine
ts status
ts ip -4
```

Use this wrapper for the custom socket; the plain CLI targets the default system
socket. Host DNS is left unchanged. Logs are in `$TAILSCALE_STATE_DIR/daemon.log`
(or the default state directory). The module installs no service on macOS;
the native Mac Tailscale application remains responsible for its connection.

This repository does not yet mount a persistent Modal volume or invoke this
command at sandbox boot; those are required for automatic recovery across
sandbox replacement.

Standalone Home Manager manages user packages on both Linux and macOS.
Edit `home-manager/home.nix` for shared packages and settings. User names,
home directories, and CPU architectures are declared in `flake.nix`.

| Profile | System | User |
| --- | --- | --- |
| `modal` | x86_64 Linux | root |
| `modal-arm` | ARM64 Linux | root |
| `mac` | Apple Silicon macOS | tomas |
| `mac-intel` | Intel macOS | tomas |

## Modal

Copy this entire `nix` directory into the image or sandbox at
`/root/.config/home-manager`. The project's Modal image already installs Nix
without systemd. Run the following as root inside that image:

```sh
export HOME=/root
export PATH="/nix/var/nix/profiles/default/bin:$PATH"
cd /root/.config/home-manager
nix flake lock
nix build '.#homeConfigurations.modal.activationPackage' --no-link
"$(nix path-info '.#homeConfigurations.modal.activationPackage')/activate"
export PATH="$HOME/.nix-profile/bin:$PATH"
home-manager --version
rg --version
```

Use `modal-arm` instead if the image uses ARM64. Save the generated `flake.lock`
alongside `flake.nix` and copy it with the configuration for repeatable builds.
The first bootstrap builds the activation package directly, so Home Manager
does not need to be installed beforehand.

For later changes:

```sh
home-manager switch --flake /root/.config/home-manager#modal
```

Modal commands do not necessarily load shell startup files. For packages to
be available to every command in a baked image, add `/root/.nix-profile/bin`
to that image's `PATH` after activation, alongside the existing Nix path.
An activation done only in a running sandbox does not change the published image.

## macOS later

Install Nix on the Mac first, with flakes enabled. Change the Mac user and home
directory in `flake.nix` if needed. Copy this directory, including `flake.lock`,
to `~/.config/home-manager`, then run as your normal user, without sudo:

```sh
cd ~/.config/home-manager
nix build '.#homeConfigurations.mac.activationPackage' --no-link
"$(nix path-info '.#homeConfigurations.mac.activationPackage')/activate"
export PATH="$HOME/.nix-profile/bin:$PATH"
home-manager --version
```

Use `mac-intel` on an Intel Mac. To load managed environment variables in future
shells, source this from your shell startup file after Nix's own initialization:

```sh
. "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh"
```

Apply later changes with `home-manager switch --flake ~/.config/home-manager#mac`.
If these files are inside a Git repository, stage new Nix files before building:
flakes only include files known to Git.

## Updates

Run `nix flake update` in this directory to refresh pinned dependencies, then
build and activate the appropriate profile. Keep `home.stateVersion` unchanged
unless deliberately migrating its compatibility settings.

This configuration does not manage shell configuration files or macOS system
settings. See the [Home Manager manual](https://nix-community.github.io/home-manager/nix-flakes.html).

## Editor: Bun, Python scripts, and Bash

Nixvim uses the TypeScript language server for Bun projects. Install the Bun
API types in each project with `bun add -d @types/bun`, and include
`"types": ["bun"]` in `tsconfig.json` under `compilerOptions` (merge with any
existing types). `bun init` provides a starting configuration for new projects.
The types belong to the project so they can follow its Bun version.

Python uses ty for completion, navigation, and type checking, plus Ruff for
linting. ty's `experimental.useUv = "scripts"` integration reads PEP 723 inline
metadata and uses uv to prepare each script's environment. It can download
interpreters and install declared dependencies when editing scripts. Cache
paths are discovered by uv rather than hardcoded to a cache layout/version.
This integration is preview and needs ty >= 0.0.76 and uv >= 0.12.3.

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///

import httpx

response = httpx.get("https://example.com")
print(response.status_code)
```

Save the script before expecting dependency discovery. In Nixvim, completion
on `httpx.` and hover on `httpx.get` should use that script's environment.
For ordinary uv projects, run `uv sync` to create the project's `.venv`.

Bash uses bash-language-server with ShellCheck available to the editor.
Use a `.sh` filename or a Bash shebang so Neovim detects the file as shell code.

References: [Bun TypeScript](https://bun.sh/docs/typescript),
[ty script environments](https://docs.astral.sh/ty/scripts/).
