# Portable Home Manager

Standalone Home Manager manages user packages and settings for the Modal
sandboxes and for a Mac. Each profile is one output of `flake.nix`.

| Profile | System | User | Entry point |
| --- | --- | --- | --- |
| `modal` | x86_64 Linux | root | `home-manager/profiles/sandbox.nix` |
| `modal-arm` | ARM64 Linux | root | `home-manager/profiles/sandbox.nix` |
| `mac` | Apple Silicon macOS | tomas | `home-manager/profiles/mac.nix` |

## Layout

| Path | Contents |
| --- | --- |
| `flake.nix` | Pinned inputs, the profile table, the formatter, and the checks |
| `home-manager/home.nix` | Modules every profile shares |
| `home-manager/profiles/` | One file per kind of machine: user, home directory, and what only that machine needs. `identity.nix` holds the Git name and emails |
| `home-manager/modules/` | One concern per file: tools, language servers, Git, Claude Code, 1Password, the editor |
| `home-manager/modules/sandbox/` | Modules only the sandbox profile imports: Tailscale, the 1Password token file, Claude's sandbox brief |
| `home-manager/modules/scripts/` | Shell scripts that modules package |

To add a tool everywhere, add it to `modules/tools.nix`. To add it to
sandboxes only, add it to `profiles/sandbox.nix`. To add a kind of machine, add
a profile file and one line to `homes` in `flake.nix`. All profiles use one
nixpkgs revision, including the editor, so a package appears in the image once.

## What a sandbox gets

- Tools for search, data, networking, archives, builds, Python, JavaScript,
  shell, and Nix, listed in `modules/tools.nix`.
- Language servers for Python (ty and Ruff), TypeScript, Bash, Lua, HTML, CSS,
  JSON, YAML, and Nix, listed in `modules/language-servers.nix`. The editor and
  Claude's LSP tool run the same builds. Claude has the Python, TypeScript,
  Bash, and Nix servers configured.
- `nix run nixpkgs#NAME`, `nix shell nixpkgs#NAME`, and `nix-shell -p NAME`
  resolve to the revision the image was built from. A tool that is not
  installed runs after a download of a few seconds and matches the libraries of
  the installed ones.
- direnv with nix-direnv. An SSH session loads a project's `.envrc` on `cd`
  after `direnv allow`. Commands run without an interactive shell use
  `direnv exec . COMMAND`.
- A managed Bash setup for root, so SSH sessions get the same `PATH`, editor,
  and locale as `sandbox exec`. This holds for an interactive login, for
  `ssh HOST COMMAND`, and for `ssh HOST 'bash -lc COMMAND'`. All three start in
  the working directory. The image sets `LANG=C.UTF-8`.
- `~/.claude/CLAUDE.md`, generated from `modules/sandbox/claude-context.md`. It
  tells Claude how the sandbox works: where to work, how to get a missing tool,
  how secrets are reached, and how the environment is changed.
- Claude starts without the 1Password token in its environment, so the
  commands it runs do not inherit it and get secrets through `op`. A rule also
  stops Claude reading the token file. The commands still run as root, so this
  prevents accidental exposure and is not a security boundary.

## Checks and formatting

There is no Nix on the Mac, so run these in a sandbox, from
`/root/.config/home-manager` or a checkout of this directory:

```sh
nix fmt
nix flake check
```

`nix fmt` formats every Nix file with nixfmt. `nix flake check` verifies the
formatting, runs statix and deadnix, and builds the profiles of the current
system. Add `--all-systems` to evaluate the Mac and ARM profiles too.

## Modal

`uv run agentix image build` copies this directory to
`/root/.config/home-manager` in the image, builds the `modal` profile, activates
it, and removes the build inputs from the Nix store. See the
[project README](../README.md). Nothing below is needed for a normal build.

To try a change in a running sandbox before rebuilding the image:

```sh
home-manager switch --flake /root/.config/home-manager#modal
```

The first switch downloads the build inputs the image left out. Use
`modal-arm` if the image uses ARM64. A switch changes that sandbox only; the
published image changes when it is rebuilt.

Modal commands do not load shell startup files. The image therefore sets
`PATH`, `EDITOR`, `VISUAL`, `LANG`, and `NIX_PATH` itself, in
`src/agentix/image.py`.

## macOS later

Install Nix on the Mac first, with flakes enabled. Change the user and home
directory in `home-manager/profiles/mac.nix` if needed. Copy this directory,
including `flake.lock`, to `~/.config/home-manager`, then run as your normal
user, without sudo:

```sh
cd ~/.config/home-manager
nix build '.#homeConfigurations.mac.activationPackage' --no-link
"$(nix path-info '.#homeConfigurations.mac.activationPackage')/activate"
export PATH="$HOME/.nix-profile/bin:$PATH"
home-manager --version
```

The Mac profile does not manage shell startup files or macOS system settings.
To load managed environment variables in future shells, source this from your
shell startup file after Nix's own initialization:

```sh
. "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh"
```

Apply later changes with `home-manager switch --flake ~/.config/home-manager#mac`.
If these files are inside a Git repository, stage new Nix files before building:
flakes only include files known to Git. Intel Macs are not supported.

## Updates

Run `nix flake update` in this directory, in a sandbox, to refresh the pinned
dependencies. Copy the new `flake.lock` back to the checkout, commit it, and
rebuild the image. Keep `home.stateVersion` unchanged unless deliberately
migrating its compatibility settings. The Nix version itself is pinned by
`NIX_INSTALLER` in `src/agentix/image.py`.

See the [Home Manager manual](https://nix-community.github.io/home-manager/nix-flakes.html).

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
(or the default state directory). Only the sandbox profile includes this
module; the native Mac Tailscale application remains responsible for its
connection.

This repository does not yet mount a persistent Modal volume or invoke this
command at sandbox boot; those are required for automatic recovery across
sandbox replacement.

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
