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
| `flake.nix` | Pinned inputs, the profile table, the exported modules, the formatter, and the checks |
| `home-manager/exports.nix` | The modules the flake exports as `homeManagerModules`, and `default`, which imports them all |
| `home-manager/home.nix` | Settings every profile here shares, including which unfree packages to allow |
| `home-manager/profiles/` | One file per kind of machine: user, home directory, the modules it turns on, and what only that machine needs |
| `home-manager/users/` | One file per person: the Git name and emails |
| `home-manager/modules/` | One concern per file: tools, language servers, Git, Claude Code, 1Password, the editor. `core.nix` declares the options several modules share |
| `home-manager/modules/sandbox/` | Modules for root in a Modal sandbox: Tailscale, Rex, the 1Password token file, Claude's sandbox brief |
| `home-manager/modules/scripts/` | Shell scripts that modules package |

Each home in `flake.nix` combines every module, a profile, and a user file.
To add a tool everywhere, add it to the list in `modules/tools.nix`. To add it
to sandboxes only, add it to `profiles/sandbox.nix`. To add a kind of machine,
add a profile file and one entry to `homes` in `flake.nix`. All profiles use
one nixpkgs revision, including the editor, so a package appears in the image
once.

## Modules and options

Every module has a switch. The shared modules start on, and the sandbox modules
follow `agentix.sandbox.enable`, which `profiles/sandbox.nix` turns on.

| Option | Module | Turns on |
| --- | --- | --- |
| `agentix.op.enable` | `1password.nix` | `op` on PATH |
| `agentix.claude.enable` | `claude-code.nix` | Claude Code, its settings and plugins, and the `claude` wrapper |
| `agentix.git.enable` | `git.nix` | Git, with `gh` and `gh-lf` as HTTPS credential helpers |
| `agentix.languageServers.enable` | `language-servers.nix` | The language servers, on PATH and in Claude's LSP tool |
| `agentix.nixvim.enable` | `nixvim.nix` | Neovim, configured with Nixvim |
| `agentix.tools.enable` | `tools.nix` | The tools in `agentix.tools.packages`, and direnv |
| `agentix.sandbox.claude.enable` | `sandbox/claude.nix` | Claude's sandbox brief and trusted directories |
| `agentix.sandbox.opToken.enable` | `sandbox/op-token.nix` | An `op` that loads the token file a sandbox stores |
| `agentix.sandbox.rex.enable` | `sandbox/rex.nix` | Rex, its terminfo entry, and its ssh guard |
| `agentix.sandbox.tailscale.enable` | `sandbox/tailscale.nix` | Tailscale and the `ts` wrapper |

Settings that a person or another module provides:

- `agentix.identity.name` and `agentix.identity.email`: who Git records as the
  author. `users/tomas.nix` sets them; another person adds a file of their own
  to `users/` and names it as `user` in `homes`.
- `agentix.tools.packages`: the tools. A list in another module adds to it;
  `lib.mkForce` replaces it.
- `agentix.unfreePackages`: the unfree packages the modules install. A module
  that adds one lists its name here, and `home.nix` allows exactly these.
- `agentix.op.package`, `agentix.op.tokenFile`, and
  `agentix.claude.trustedDirectories`, which the sandbox modules set.

To add a module here:

1. Create `modules/NAME.nix` with an `agentix.NAME.enable` option, made with
   `lib.mkEnableOption` and `default = true` for a shared module, or
   `default = config.agentix.sandbox.enable` and an import of `./options.nix`
   for a sandbox one. Put its settings under `lib.mkIf` of that option.
2. Import the files whose options it reads, such as `./1password.nix` for
   `agentix.op.package`. Importing a file more than once is fine.
3. If it uses a flake input, write it as `{ inputs }: { config, lib, ... }: ...`
   and list it with `withInputs` in `exports.nix`. Otherwise list the path.
   Add it to `default` there and give it a name of its own.
4. List any unfree package it installs in `agentix.unfreePackages`.
5. Stage the new file (flakes only see files known to Git) and run
   `nix flake check`.

## Use from another flake

The flake exports `homeManagerModules.default`, which imports every module,
and one module per concern: `"1password"`, `claude-code`, `git`,
`language-servers`, `nixvim`, `tools`, `sandbox-claude`, `sandbox-op-token`,
`sandbox-rex`, and `sandbox-tailscale`. Each brings the modules it depends on.
The modules use this flake's pinned inputs, not those of the flake that imports
them.

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager/master";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    agentix = {
      url = "github:thehumanworks/agentix?dir=nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.home-manager.follows = "home-manager";
    };
  };

  outputs =
    { nixpkgs, home-manager, agentix, ... }:
    {
      homeConfigurations.me = home-manager.lib.homeManagerConfiguration {
        pkgs = nixpkgs.legacyPackages.x86_64-linux;
        modules = [
          agentix.homeManagerModules.default
          ./my-module.nix
          (
            { config, lib, ... }:
            {
              home = {
                username = "me";
                homeDirectory = "/home/me";
                stateVersion = "26.05";
              };
              agentix.identity = {
                name = "Me";
                email = "me@example.com";
              };
              agentix.nixvim.enable = false;
              # The modules do not change nixpkgs settings. Allow their list.
              nixpkgs.config.allowUnfreePredicate =
                pkg: builtins.elem (lib.getName pkg) config.agentix.unfreePackages;
            }
          )
        ];
      };
    };
}
```

Without the unfree setting, evaluation refuses `claude-code` and
`1password-cli`. With Home Manager inside NixOS and `useGlobalPkgs`, allow
`config.home-manager.users.NAME.agentix.unfreePackages` in the system's own
predicate instead. The
`nixvim` and `sandbox-rex` modules import the Home Manager modules of Nixvim
and Rex; do not import those again. The gh wrappers, the Claude login, and
Tailscale read secrets from the `agentix` vault in 1Password; turn off the
modules you have no such items for.

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
- Rex, the terminal multiplexer, with its terminfo entry and ssh guard. See
  [Rex in sandboxes](#rex-in-sandboxes).

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

From the repository root, `uv run agentix flake update` moves every pinned
input to its newest revision, and `uv run agentix flake update claude-mods`
moves only the named ones. The command runs `nix flake update` in a sandbox, so
the host needs no Nix, then writes the new `flake.lock` to this directory and
lists the inputs that moved. Add `--build` to build and publish the image in
the same run when an input moved; otherwise rebuild afterwards. Commit the
lock.

Keep `home.stateVersion` unchanged unless deliberately migrating its
compatibility settings. The Nix version itself is pinned by `NIX_INSTALLER` in
`src/agentix/image.py`.

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
(or the default state directory). Only the sandbox profile turns this module
on; the native Mac Tailscale application remains responsible for its
connection.

This repository does not yet mount a persistent Modal volume or invoke this
command at sandbox boot; those are required for automatic recovery across
sandbox replacement.

## Rex in sandboxes

`modules/sandbox/rex.nix` sets up Rex with the Home Manager module from Rex's
own flake, as on the nixos box: the `rex` command, the `xterm-rex` terminfo
entry in `~/.terminfo`, and Rex's ssh guard in `~/.ssh/config`, which Home
Manager now writes for root. The flake input follows the snapshot channel and
this flake's nixpkgs. The flake and the release binary download without
credentials, so the image build needs no secret. Move to the newest snapshot
with `uv run agentix flake update rex`, then rebuild. The Mac profile leaves
the module off; the Rex app installs `rex` and runs the server there.

The nixos box runs its server as a systemd user service with its own Tailscale
node. A sandbox has no systemd, so nothing starts at boot. The first `rex`
command starts a server in the background instead. It keeps its sessions
across `sandbox exec` calls and SSH logins, which all reach the same socket in
`~/.local/state/rex/run`, and it exits when its last session closes. For a
server that stays until the sandbox ends, run:

```sh
rex server start
```

The server does not inherit the 1Password token, so programs in its terminals
get secrets through `op`, as Claude's commands do. Terminals use
`TERM=xterm-rex`; ssh from one sends `xterm-256color` to any host not listed in
`~/.config/rex/terminfo-hosts`.

From another machine, reach the server over SSH once the sandbox has an SSH
listener (see the [project README](../README.md)):

```sh
rex -S ssh://root@HOSTNAME ls
```

Rex's own Tailscale node is not enabled: it would enroll a second tailnet
device per sandbox, and the sandbox already has one for SSH. To add it, start
the server with `rex server start --tailscale --tailscale-hostname NAME` and
pass an auth key with `--tailscale-auth-key-file`, from the sandbox startup
process.

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
