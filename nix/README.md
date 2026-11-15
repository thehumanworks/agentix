# Portable Home Manager

Standalone Home Manager manages user packages and settings for the Modal
sandboxes, for any user on any Linux machine or container, and for a Mac.
Each profile is one output of `flake.nix`.

| Profile | System | User | Entry point |
| --- | --- | --- | --- |
| `modal` | x86_64 Linux | root | `home-manager/profiles/sandbox.nix` |
| `modal-arm` | ARM64 Linux | root | `home-manager/profiles/sandbox.nix` |
| `linux` | x86_64 Linux | agent, an example | `home-manager/profiles/linux.nix`, through `lib.mkLinuxHome` |
| `mac` | Apple Silicon macOS | tomas | `home-manager/profiles/mac.nix` |

`linux` is an example; for your own user, see [Any Linux user](#any-linux-user).

## Layout

| Path | Contents |
| --- | --- |
| `flake.nix` | Pinned inputs, the profile table, `lib.mkLinuxHome`, the exported modules, the OCI images, the formatter, and the checks |
| `sandbox.toml` | The paths that the agentix command on the host and the image share, where the manifest goes, and the image's environment |
| `home-manager/exports.nix` | The modules the flake exports as `homeManagerModules`, and `default`, which imports them all |
| `home-manager/home.nix` | Settings every profile here shares, including which unfree packages to allow |
| `home-manager/profiles/` | One file per kind of machine: user, home directory, the modules it turns on, and what only that machine needs. `linux.nix` holds what every Linux machine gets, and `sandbox.nix` builds on it |
| `home-manager/users/` | One file per person: the Git name and emails |
| `home-manager/modules/` | One concern per file: tools, language servers, Git, Claude Code, Codex, pi, 1Password, the editor, comma and nix-index. `op-run.nix` builds the harness wrappers that get their credentials from 1Password. `core.nix` declares the options several modules share |
| `home-manager/modules/sandbox/` | Modules for root in a Modal sandbox: the base commands, the C compiler, the headless browser, OpenSSH, Tailscale, Rex, the 1Password token file, Claude's sandbox brief, the SSH working directory, the manifest |
| `home-manager/modules/scripts/` | Shell scripts that modules package, among them `agentix-sync.sh`, which the host's agentix command also runs |
| `packages/` | Packages the flake builds on every system: `agentix-sync` (`nix run .#agentix-sync`) |
| `oci/` | The sandbox profile as an OCI image, which the Modal image is made from; see [OCI image](#oci-image) |

Each home in `flake.nix` combines every module, a profile, and a user file.
To add a tool everywhere, add it to the list in `modules/tools.nix`. To add it
to every Linux home, add it to `profiles/linux.nix`; to sandboxes only, to
`profiles/sandbox.nix`. To add a kind of machine,
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
| `agentix.codex.enable` | `codex.nix` | The Codex CLI and the `codex` wrapper, which gets `agentix.codex.secrets` from 1Password |
| `agentix.git.enable` | `git.nix` | Git, with `gh` and `gh-lf` as HTTPS credential helpers |
| `agentix.languageServers.enable` | `language-servers.nix` | The language servers, on PATH and in Claude's LSP tool |
| `agentix.nixIndex.enable` | `nix-index.nix` | comma (`, COMMAND`) and `nix-locate`, with the prebuilt index from nix-index-database, and the shell's command-not-found hook |
| `agentix.nixvim.enable` | `nixvim.nix` | Neovim, configured with Nixvim |
| `agentix.pi.enable` | `pi.nix` | The pi coding agent and the `pi` wrapper, which gets `agentix.pi.secrets` from 1Password |
| `agentix.sync.enable` | `sync.nix` | `agentix-sync`, which `agentix sandbox sync` runs on both sides to copy uncommitted changes between checkouts with Git |
| `agentix.tools.enable` | `tools.nix` | The tools in `agentix.tools.packages`, and direnv |
| `agentix.sandbox.baseCommands.enable` | `sandbox/base-commands.nix` | `tar`, `gzip`, `xz`, `less`, `which`, `flock`, `locale`, `infocmp`, and the other commands a Debian base would provide |
| `agentix.sandbox.browser.enable` | `sandbox/browser.nix` | `chrome-headless-shell`, Playwright's headless Chromium, run with `--no-sandbox` for root, and `PLAYWRIGHT_BROWSERS_PATH` for Playwright clients |
| `agentix.sandbox.cc.enable` | `sandbox/cc.nix` | GCC as `cc`, `gcc`, `c++`, and `g++`, and binutils (`ld`, `ar`, `nm`) |
| `agentix.sandbox.claude.enable` | `sandbox/claude.nix` | Claude's sandbox brief and trusted directories |
| `agentix.sandbox.manifest.enable` | `sandbox/manifest.nix` | The manifest of commands and shared paths |
| `agentix.sandbox.opToken.enable` | `sandbox/op-token.nix` | An `op` that loads the token file a sandbox stores |
| `agentix.sandbox.openssh.enable` | `sandbox/openssh.nix` | `sshd` and `ssh-keygen` for `sandbox ssh start --engine=openssh`, the `ssh`, `scp`, and `sftp` clients, and the image's `sshd` account |
| `agentix.sandbox.rex.enable` | `sandbox/rex.nix` | Rex, its terminfo entry, and its ssh guard |
| `agentix.sandbox.tailscale.enable` | `sandbox/tailscale.nix` | Tailscale, for `sandbox ssh start --engine=tailscale`, and the `ts` wrapper |
| `agentix.sandbox.workdir.enable` | `sandbox/workdir.nix` | The start of SSH sessions in the working directory |

Settings that a person or another module provides:

- `agentix.identity.name` and `agentix.identity.email`: who Git records as the
  author. `users/tomas.nix` sets them; another person adds a file of their own
  to `users/` and names it as `user` in `homes`.
- `agentix.tools.packages`: the tools. A list in another module adds to it;
  `lib.mkForce` replaces it.
- `agentix.unfreePackages`: the unfree packages the modules install. A module
  that adds one lists its name here, and `home.nix` allows exactly these.
- `agentix.commands`: the commands the modules put on PATH. The manifest lists
  them and the image build checks each. `tools.nix` takes the main command of
  each of its packages, so a tool added there is checked too.
- `agentix.sandbox.accounts`: system accounts that a module needs, such as
  `sshd`. Home Manager cannot add accounts, so the OCI image writes them to
  `/etc/passwd`, `/etc/group`, and `/etc/shadow`.
- `agentix.sandbox.paths` and `agentix.sandbox.manifestFile`: read-only, from
  `sandbox.toml`.
- `agentix.op.package`, `agentix.op.tokenFile`, and
  `agentix.claude.trustedDirectories`, which the sandbox modules set.
- `agentix.codex.secrets` and `agentix.pi.secrets`: environment variables
  that `codex` and `pi` get from 1Password, as op:// references that
  `op run` resolves at start. Both default to `op://agentix/openai/api/key`,
  as `CODEX_API_KEY` (what `codex exec` reads) and `OPENAI_API_KEY`. For pi
  with Anthropic instead, set
  `agentix.pi.secrets = { ANTHROPIC_API_KEY = "op://..."; }`; `pi --help`
  lists each provider's variable. A wrapper started with only `--version` or
  `--help` skips 1Password.

To add a module here:

1. Create `modules/NAME.nix` with an `agentix.NAME.enable` option, made with
   `lib.mkEnableOption` and `default = true`, for a shared module, or
   `modules/sandbox/NAME.nix` with `agentix.sandbox.NAME.enable`,
   `default = config.agentix.sandbox.enable`, and an import of `./options.nix`
   for a sandbox one. Put its settings under `lib.mkIf` of that option.
2. Import the files whose options it reads, such as `./1password.nix` for
   `agentix.op.package`. Importing a file more than once is fine.
3. If it uses a flake input, write it as `{ inputs }: { config, lib, ... }: ...`
   and list it with `withInputs` in `exports.nix`. Otherwise list the path.
   Add it to `default` there and give it a name of its own.
4. List any unfree package it installs in `agentix.unfreePackages`, the
   commands that agents rely on in `agentix.commands`, and any system account
   it needs in `agentix.sandbox.accounts`.
5. Stage the new file (flakes only see files known to Git) and run
   `nix flake check`.

## Shared paths and the manifest

`sandbox.toml` holds the paths that both sides of a sandbox use: the working
directory, and the files the start command writes to `/run/agentix` (the
1Password token, the `--workdir` override, and the ready file). The Home
Manager modules read it as the read-only options `agentix.sandbox.paths`, and
`src/agentix/layout.py` reads it for the command line and the image build.
Its `[env]` table is the environment of the image, which `oci/default.nix`
writes into the OCI image and the Modal build gives the Modal image. Change a
value there, then rebuild the image.

The Home Manager build writes a manifest into the profile, at
`share/agentix/manifest.json`: in a sandbox,
`/root/.nix-profile/share/agentix/manifest.json`, and in the image's
generation, `/opt/agentix/home-generation/home-path/share/agentix/manifest.json`.

```json
{
  "commands": ["ast-grep", "bash", "claude", "..."],
  "paths": {
    "opTokenFile": "/run/agentix/op-token",
    "readyFile": "/run/agentix/ready",
    "workdir": "/workdir",
    "workdirFile": "/run/agentix/workdir"
  }
}
```

The image build fails if a listed command is not in root's profile or does
not resolve to it on the image's `PATH`, or if the paths differ from the ones
`sandbox.toml` gives the host.

## Use from another flake

The flake exports `homeManagerModules.default`, which imports every module,
and one module per concern: `"1password"`, `claude-code`, `codex`, `git`,
`language-servers`, `nix-index`, `nixvim`, `pi`, `sync`, `tools`, `sandbox-base-commands`,
`sandbox-browser`, `sandbox-cc`, `sandbox-claude`, `sandbox-manifest`,
`sandbox-op-token`, `sandbox-openssh`, `sandbox-rex`, `sandbox-tailscale`, and
`sandbox-workdir`. Each brings the
modules it depends on.
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
`nixvim`, `nix-index`, and `sandbox-rex` modules import the Home Manager
modules of Nixvim, nix-index-database, and Rex; do not import those again.
The gh wrappers, the Claude login, Codex, pi, and Tailscale read secrets from the
`agentix` vault in 1Password; turn off the modules you have no such items
for.

## Any Linux user

`lib.mkLinuxHome` gives a Home Manager configuration for one user on any
Linux machine or container, with no Modal and no root. It takes the user name
and home directory, and optionally the system and extra modules:

```nix
agentix.lib.mkLinuxHome {
  username = "ada";
  homeDirectory = "/home/ada";
  system = "x86_64-linux"; # the default; "aarch64-linux" evaluates too
  modules = [
    {
      agentix.identity = {
        name = "Ada Lovelace";
        email = "ada@example.com";
      };
    }
  ];
}
```

In a flake of your own, with this flake as the input `agentix`, set
`homeConfigurations.ada` to that and run
`home-manager switch --flake .#ada`. Without a flake of your own, build and
activate it for the current user straight from this one:

```sh
nix build --impure --out-link /tmp/agentix-home --expr '
  ((builtins.getFlake "github:thehumanworks/agentix?dir=nix").lib.mkLinuxHome {
    username = builtins.getEnv "USER";
    homeDirectory = builtins.getEnv "HOME";
  }).activationPackage'
HOME_MANAGER_BACKUP_EXT=bak /tmp/agentix-home/activate
```

`USER` and `HOME` must be set; a container's shell may lack `USER`
(`export USER=$(id -un)`). From a checkout, use `path:/PATH/TO/agentix/nix`
in place of the GitHub URL.
`homeConfigurations.linux` is the same function for a user `agent` in
`/home/agent`, which `nix flake check` builds.

The configuration manages `~/.bashrc` and `~/.profile`. Home Manager will not
replace files it did not write, so on a distribution, whose new users get
both from `/etc/skel`, set `HOME_MANAGER_BACKUP_EXT=bak` (or pass
`home-manager switch -b bak`) once, and it moves them aside.

`profiles/linux.nix` holds what every Linux home gets, the sandbox included:
the shared modules, `ps` and `strace`, the Bash setup that loads the session
variables in login shells and `ssh HOST COMMAND`, the C.UTF-8 locale, a
headless editor, and `nixpkgs` pinned to this flake's revision. Of the sandbox
modules it turns on the ones that work for any user: the base commands, the
manifest, Rex, and OpenSSH. On a distribution, the base commands shadow its
own `tar`, `less`, `which`, `flock`, and the rest, because
`~/.nix-profile/bin` comes before `/usr/bin` on PATH; their low priority only
decides between packages within the profile. Set
`agentix.sandbox.baseCommands.enable = false` to keep the distribution's. The
rest depend on what `agentix sandbox up` sets up in a Modal sandbox, or on
root, and stay off:

| Module | In a sandbox | Without it |
| --- | --- | --- |
| `opToken` | `op` loads the token that `sandbox up` writes to `/run/agentix/op-token`, and Claude may not read the file | The stock `op`: it signs in as usual, or uses `OP_SERVICE_ACCOUNT_TOKEN` from the environment |
| `workdir` | SSH sessions start in `/workdir`, or in the directory `/run/agentix/workdir` names | Sessions start in the home directory |
| `claude` | `~/.claude/CLAUDE.md` describes the sandbox, and Claude trusts the home and `/workdir` | No sandbox brief; Claude asks before trusting a directory |
| `tailscale` | `ts`, which runs `tailscaled` as root | Run Tailscale the way the machine does |

The manifest still lists the shared paths from `sandbox.toml`, which nothing
reads outside a sandbox. Turn any module on in `modules`, or all of them with
`agentix.sandbox.enable = true`, as for root in a container that the agentix
command does not start. `nix flake check` fails if the `linux` example loads
the token file, starts in the working directory, has the sandbox brief or
`ts`, or lacks `claude`, `git`, `nvim`, `op`, `rex`, or `ssh`.

The secrets that the `claude`, `codex`, `pi`, `gh`, and `gh-lf` wrappers read come from the
`agentix` vault in 1Password, as in a sandbox. With no access to it, sign `op`
in to an account that has the items, or turn those modules off:
`agentix.claude.enable = false` removes Claude Code,
`agentix.codex.enable = false` and `agentix.pi.enable = false` remove Codex
and pi, and
`agentix.git.enable = false` removes Git itself and its configuration along
with the `gh` and `gh-lf` wrappers.

## What a sandbox gets

- Tools for search, data, networking, archives, builds, Python, JavaScript,
  shell, and Nix, listed in `modules/tools.nix`, including `psql`,
  `hyperfine`, `watchexec`, `difft` (difftastic), and `nom`
  (nix-output-monitor).
- The commands of a Debian system that agents and the scripts here use:
  `tar`, `gzip`, `xz`, `bzip2`, `less`, `which`, `flock` and the rest of
  util-linux, `locale`, `iconv`, `infocmp`, `tput`, `openssl`, `hostname`, and
  `getent`, listed in `modules/sandbox/base-commands.nix`. They come from the
  pinned nixpkgs, as everything in the image does.
- A C and C++ compiler: GCC as `cc`, `gcc`, `c++`, and `g++`, with binutils,
  from `modules/sandbox/cc.nix`. It finds glibc's headers and libraries on its
  own; other libraries come from a project's Nix shell.
- A headless Chromium, Playwright's `chrome-headless-shell`, from
  `modules/sandbox/browser.nix`. On PATH it runs with `--no-sandbox`, which
  Chromium needs as root: `chrome-headless-shell --dump-dom URL`,
  `--screenshot=FILE`, `--print-to-pdf=FILE`. The image's
  `PLAYWRIGHT_BROWSERS_PATH` points Playwright clients of the same minor
  release as nixpkgs' (in `$PLAYWRIGHT_BROWSERS_PATH/VERSION`) at it: Node's
  `bunx playwright@1.64`, or nixpkgs' `python3Packages.playwright`. The PyPI
  wheel under `uv` does not load, because greenlet needs a libstdc++ the
  image has no system copy of. Other releases look for a build of their own.
  It starts with the DejaVu fonts, so CJK text renders as boxes. The full
  Chromium would add about 1.7 GiB; this build adds about 500 MiB. Running
  as root with `--no-sandbox` leaves the sandbox as its only isolation, so
  avoid untrusted sites in sandboxes that hold tokens. With the module off,
  remove `PLAYWRIGHT_*` from `[env]` in `sandbox.toml`; the image build fails
  while `PLAYWRIGHT_BROWSERS_PATH` names no browser.
- comma and `nix-locate`, from `modules/nix-index.nix`. `, COMMAND ARGS` looks
  COMMAND up in a prebuilt index of nixpkgs, the small database of the
  `nix-index-database` input, and runs it from the image's `nixpkgs` without
  installing it. No index is built at run time. The index is of
  nixpkgs-unstable in the week the input was locked, so a package that the
  pinned revision lacks may be named; `nix flake update nix-index-database`
  moves it. When several packages have COMMAND, comma asks with fzy if
  standard input and error are a terminal and `COMMA_NONINTERACTIVE` is not
  1. Otherwise it runs the package whose attribute is COMMAND, or lists the
  candidates and fails. comma remembers choices in `~/.cache/comma`, which
  sandbox snapshots keep; agents set `COMMA_CACHING=0` (or pass
  `--cache-level 0`) to leave no record.
- Measured when they were added, the compiler, the browser, comma, and the
  tools added with them (`psql`, `hyperfine`, `watchexec`, `difft`, `nom`)
  took the unpacked image from 2.0 to 3.0 GiB: GCC and binutils about
  330 MiB, the browser and its libraries about 500 MiB, difftastic 86 MiB,
  PostgreSQL 26 MiB, and the rest under 25 MiB together. Turn a module off
  to leave its share out.
- Two more agent harnesses besides Claude Code, from nixpkgs: the Codex CLI
  (`codex`, `modules/codex.nix`) and pi, the coding agent from pi.dev
  (`pi`, nixpkgs' `pi-coding-agent`, `modules/pi.nix`). Each wrapper gets
  its API key from 1Password when it starts. Codex's own command sandbox
  works in a Modal sandbox, so its settings keep the default sandbox modes.
  They took the unpacked image from 3.0 to 3.8 GiB: Codex about 410 MiB
  plus about 110 MiB of the GStreamer libraries for its voice mode, and pi
  about 240 MiB. On Linux the module strips Codex's binaries, which nixpkgs
  leaves with their symbol tables, and drops its `logs_client` (about
  195 MiB less; `codex doctor` reports the same). Turn
  `agentix.codex.enable` or `agentix.pi.enable` off to leave either out.
- Language servers for Python (ty and Ruff), TypeScript, Bash, Lua, HTML, CSS,
  JSON, YAML, and Nix, listed in `modules/language-servers.nix`. The editor and
  Claude's LSP tool run the same builds. Claude has the Python, TypeScript,
  Bash, and Nix servers configured.
- `, COMMAND`, `nix run nixpkgs#NAME`, `nix shell nixpkgs#NAME`, and
  `nix-shell -p NAME` resolve to the revision the image was built from. A
  tool that is not installed runs after a download of a few seconds and
  matches the libraries of the installed ones.
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
- OpenSSH and Tailscale, for the two SSH engines of `agentix sandbox ssh`,
  each in a module of its own. See [SSH engines](#ssh-engines).
- Rex, the terminal multiplexer, with its terminfo entry and ssh guard. See
  [Rex in sandboxes](#rex-in-sandboxes).
- `agentix-sync`, from `modules/sync.nix` and `packages/agentix-sync.nix`,
  the side of `agentix sandbox sync` that runs here. The host's agentix
  command runs the same script from `modules/scripts/agentix-sync.sh`, so
  both sides have one copy of the code. Run by hand, `agentix-sync undo DIR`
  puts back the working tree a sync replaced.
- `~/.nix-profile/share/agentix/manifest.json`, which lists the commands the
  configuration provides and the paths it shares with the host. See
  [Shared paths and the manifest](#shared-paths-and-the-manifest).

## Checks and formatting

There is no Nix on the Mac, so run these in a sandbox, from
`/root/.config/home-manager` or a checkout of this directory:

```sh
nix fmt
nix flake check
```

`nix fmt` formats every Nix file with nixfmt. `nix flake check` verifies the
formatting, runs statix and deadnix, and builds the profiles of the current
system, the `linux` example included on x86_64. On Linux it also evaluates
the sandbox profile with each SSH engine's module turned off in turn, and both
off; see [SSH engines](#ssh-engines). On Linux it also builds the OCI image's
activated root and layer list,
which takes seconds once the profile is built, but not the image stream itself.
Add `--all-systems --no-build` to evaluate the Mac and ARM profiles and the ARM
image too; without `--no-build`, Nix stops at the first check it cannot build
on this system. CI runs both, and runs `nix flake check` on an ARM runner as
well; see [ARM Linux](#arm-linux).

## Modal

`uv run agentix image build` publishes the [OCI image](#oci-image) of this
directory as a Modal image; see the [project README](../README.md). Nothing
below is needed for a normal build.

Modal's image builder cannot build the OCI image itself: the activation under
proot crashes there, and so do some builds of the stock Nix. The build
therefore runs in a temporary VM sandbox started from the official image of
the Nix release that `NIX_BUILDER` in `src/agentix/image.py` pins by digest.
The sandbox gets this directory, runs `nix build .#oci`, and unpacks the
image's layers, lowest first, into a directory. A snapshot of that directory
is the root of the Modal image, so nothing of the builder is in it. The build
adds the `[env]` of `sandbox.toml`, the working directory, and `sleep
infinity` as the command for a sandbox started without one, then runs its
checks in a layer it does not publish.

The OCI image's entry point is left out on Modal, because Modal would run
every sandbox's start command through it. The start command in
`src/agentix/sandbox.py` does the same work there: the token file, the
`--workdir` file, and the ready file.

Each unpacked root is kept as the Modal image `agentix-rootfs:HASH`, where
the hash covers the files of this directory, `NIX_BUILDER`, and the build
script. A build from the same inputs reuses it, so only the Modal layers are
built again; `--force-build` builds the root again too. Without such a hit,
the build starts from an empty store and takes about two minutes. The roots
stay until deleted with `modal.experimental.image_delete`; a published image
is built on its root, so delete a root only after nothing uses it. Deleting
an image leaves its name, which `modal image names delete` removes; a build
that finds a deleted root under its name builds the root again.

To try a change in a running sandbox before rebuilding the image:

```sh
home-manager switch --flake /root/.config/home-manager#modal
```

The first switch downloads the build inputs the image left out. The Modal
image is always x86_64, so switch to `modal`: `image build` builds only
`packages.x86_64-linux.oci` and cannot select another system; `modal-arm`
is for [ARM Linux](#arm-linux) hosts. A switch changes that sandbox only; the
published image changes when it is rebuilt.

Modal commands do not load shell startup files, so the image's environment
gives them `PATH`, `HOME`, `USER`, `LOGNAME`, `EDITOR`, `VISUAL`, `LANG`,
`NIX_PATH`, `IS_SANDBOX`, and `SSL_CERT_FILE`, from `sandbox.toml`. Modal
also sets `SSL_CERT_DIR` to `/etc/ssl/certs` for the commands it starts,
which holds only the CA bundle; with `SSL_CERT_FILE`, `curl` finds the
bundle there too.

## OCI image

`packages.x86_64-linux.oci` and `packages.aarch64-linux.oci` build the same
sandbox as an OCI image with Nix alone: no base image and no apt. It runs
under Docker, Podman, a devcontainer, or another cloud. The Modal image is
the x86_64 one; see [Modal](#modal).

Build it on a Linux machine with Nix, or in a sandbox, from this directory.
The result is a script that streams the image, so the image never lands in
the store as one large file:

```sh
nix build .#oci -o /tmp/oci     # x86_64; .#packages.aarch64-linux.oci on ARM
/tmp/oci | docker load          # or podman load; prints agentix-sandbox:latest
/tmp/oci | zstd > agentix-sandbox.tar.zst   # a file to copy elsewhere
```

Keep the link outside this directory, or add it to `.gitignore`: a `result`
link inside an unversioned copy would become part of the flake. The image
is about 2.0 GB unpacked, in 99 layers. The layers follow store paths, so a
rebuild after a small change reuses most of them. Two builds of one commit
produce the same image. The ARM image needs an ARM Linux builder; see
[ARM Linux](#arm-linux).

On an Apple Silicon Mac, Docker runs the x86_64 image under qemu emulation.
Commands work, slowly, including `nix run`. Interactive shells do not: qemu
hides the terminal from the container, so without a command it waits as if
detached. The ARM image is meant for that case, but nobody has run it yet.

Run it:

```sh
docker run -it --rm agentix-sandbox                      # login shell in /workdir
docker run --rm agentix-sandbox nix run nixpkgs#hello   # one command
docker run -d --name dev -e OP_SERVICE_ACCOUNT_TOKEN agentix-sandbox
docker exec -it dev bash -l                             # into the running one
```

The entry point does what `agentix sandbox up` does in its start command.
It stores `OP_SERVICE_ACCOUNT_TOKEN` in `/run/agentix/op-token`, readable only
by root, and records a working directory set with `-w PATH` for SSH logins.
`-e OP_SERVICE_ACCOUNT_TOKEN` without a value copies the variable from your
shell, so the token stays off the command line. A container started without
a command opens a login shell when it has a terminal. Without a terminal it
stays up for `docker exec`, as a Modal sandbox does. Any other command runs as
given, with the environment in `sandbox.toml`, as on Modal.

As on Modal, processes that `docker run` and `docker exec` start get the token
from the environment. SSH sessions and Claude's commands get it from the file
through `op`, and Claude starts without it. Docker keeps `/run` on disk, in
the container's writable layer, so the entry point keeps `/run/agentix` in
`/dev/shm`, which is in memory. With `--tmpfs /run` it uses `/run` itself. A
container started with another `--entrypoint` gets no token file. The runtime
stores the variables passed with `-e`, and `docker inspect` shows them.

The image holds:

- Root's Home Manager generation, activated while the image is built. Home
  Manager's own activation script runs, with stand-ins for the Nix commands
  it calls; see `oci/activation-shim.sh`. A Home Manager update that needs
  more from Nix fails the build.
- Root's Nix profile, laid out as `nix profile add` lays it out: Nix, the
  Home Manager packages, which include the base commands such as `tar`,
  `less`, `flock`, and `locale`, CA certificates, and glibc's other commands,
  such as `ldd`. The Nix database
  registers every store path in the image, so `nix run`, `nix profile`,
  `home-manager switch`, and `nix store gc` work and keep the image intact.
- This flake in `/root/.config/home-manager`, as on Modal.
- `/etc` files for root, `nobody`, and the accounts in
  `agentix.sandbox.accounts`, which is `sshd` while the OpenSSH module is on,
  `/bin/sh`, `/usr/bin/env`, the
  dynamic loader at its usual path for binaries built elsewhere, and
  `/workdir`. `/etc/bashrc` puts the Nix profile on `PATH` for
  `ssh HOST COMMAND`, as the Nix installer's hook does on other distributions.

The entry point, the working directory, and Claude's deny rule use the paths
in `sandbox.toml`. The build fails unless every command in the manifest is in
root's profile.

The Modal image was built on Debian 12 with the Determinate Nix installer
until it came from this image. Compared with that image:

- Nix is the pinned nixpkgs' release, 2.35, not Determinate Nix 3, so
  Determinate's own settings, such as `eval-cores` and `lazy-trees`, are
  unknown to it.
- Debian's own commands are gone: `apt` and `dpkg`, `perl`, `gpg`, `su`,
  `useradd` and the rest of the shadow tools, the ext4 tools, `dash`, and
  `run-parts`. Run any of them from nixpkgs, for example
  `nix shell nixpkgs#gnupg -c gpg --version`. Git's own Perl scripts carry
  their Perl.
- The `sshd` account is uid 74 with `/var/empty` as its home, not uid 999.
- The CA certificates are nixpkgs' bundle, in
  `/etc/ssl/certs/ca-certificates.crt`, not one file per certificate.
- The prompt, `user@host:directory#`, comes from Home Manager's `.bashrc`, not
  from Debian's `/etc/bash.bashrc`.
- `~/.claude/CLAUDE.md` says that there is no distribution under the sandbox.

A minimal `.devcontainer/devcontainer.json` for the loaded image:

```json
{
  "image": "agentix-sandbox:latest",
  "overrideCommand": false,
  "workspaceFolder": "/workdir",
  "workspaceMount": "source=${localWorkspaceFolder},target=/workdir,type=bind",
  "containerEnv": { "OP_SERVICE_ACCOUNT_TOKEN": "${localEnv:OP_SERVICE_ACCOUNT_TOKEN}" }
}
```

`overrideCommand: false` keeps the image's entry point, which writes the token
file and keeps the container up. Without it, the devcontainer tools replace
the entry point.

## ARM Linux

`modal-arm` is the sandbox profile, `home-manager/profiles/sandbox.nix`, for
aarch64 Linux: the same modules and settings as `modal`, for root, from the
same pins. `packages.aarch64-linux.oci` is its OCI image, for ARM Linux hosts
and for Docker on an Apple Silicon Mac. Modal never uses either; the Modal
image is the x86_64 one.

CI checks both. `nix flake check --all-systems --no-build` evaluates them on
x86_64, and the `Nix flake check (ARM)` job runs `nix flake check` on an
`ubuntu-24.04-arm` runner, which builds the profile, the image's activated
root and layer list, and the SSH engine checks. That job is new: it is
expected to pass from its first run but has not yet been seen green. The ARM
OCI image has not been built anywhere else, and nothing runs it yet.

On x86_64 Linux, the profile builds under qemu-user emulation. This is how it
was first built, in a Modal VM sandbox from the `NIX_BUILDER` image, in
48 seconds from an empty store (64 local builds; the rest came from
cache.nixos.org):

```sh
mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc  # if not mounted
qemu=$(nix build --no-link --print-out-paths --inputs-from . nixpkgs#qemu-user)
printf '%s' ":qemu-aarch64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:$qemu/bin/qemu-aarch64:FP" \
  > /proc/sys/fs/binfmt_misc/register
nix build --option extra-platforms aarch64-linux \
  .#homeConfigurations.modal-arm.activationPackage
```

Keep the `P` flag, which passes the program's own name to qemu. Without it,
Python environments lose their packages under emulation, and the unfree
binaries, which Nix builds here because cache.nixos.org does not serve them,
fail in `autoPatchelfHook` with `No module named 'elftools'`. NixOS's
`boot.binfmt.emulatedSystems` sets it too. The OCI image does not build under
qemu-user: its fakechroot step runs proot, which needs `ptrace`, and qemu-user
does not provide it. Build the image on an ARM machine.

proot on aarch64 does not translate the path of the access check that
`test -x` makes, while stat and readlink work. So the image build's checks
test commands through their resolved store paths; a plain
`test -x /root/.nix-profile/bin/NAME` fails there for every command. This
was found and the fix confirmed by running the image's customisation step,
proot and fakeroot included, in an aarch64 NixOS VM under qemu-system.

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
input to its newest revision except Rex, and `uv run agentix flake update
claude-mods` moves only the named ones. Rex moves only when named, as in
`uv run agentix flake update rex`; see [Rex in sandboxes](#rex-in-sandboxes).
The command runs `nix flake update` in a sandbox, so the host needs no Nix,
and evaluates the `modal` profile with the new lock there, which fetches every
input from the URL the lock records. Only when that succeeds does it write the
new `flake.lock`, and `flake.nix` when Rex moved, to this directory and list
the inputs that moved. Add `--build` to build and publish the image in the
same run when an input moved; otherwise rebuild afterwards. Commit what
changed.

Keep `home.stateVersion` unchanged unless deliberately migrating its
compatibility settings. The Nix that builds the image is pinned by
`NIX_BUILDER` in `src/agentix/image.py`, whose comment says how to move it;
CI installs the same release. Moving it builds the image's root again, but
the image's own Nix follows `flake.lock`.

See the [Home Manager manual](https://nix-community.github.io/home-manager/nix-flakes.html).

## SSH engines

`agentix sandbox ssh start` reaches a sandbox through one of two engines,
and each has a module: `--engine=tailscale`, the default, needs
`agentix.sandbox.tailscale`, and `--engine=openssh` needs
`agentix.sandbox.openssh`. Both follow `agentix.sandbox.enable`, so the
sandbox profile has both, and either can be turned off without the other:

| Tailscale | OpenSSH | The image has |
| --- | --- | --- |
| on | on | Both engines, `ts`, `tailscale`, `sshd`, `ssh-keygen`, the `ssh` client, and the `sshd` account |
| on | off | Tailscale SSH and `ts`, but no `sshd`, `ssh-keygen`, or `ssh` on `PATH`, and no `sshd` account |
| off | on | OpenSSH, but no `ts` or `tailscale` |
| off | off | No SSH engine; `sandbox exec` still works |

Tailscale SSH's server is part of `tailscaled` and needs nothing from
OpenSSH, `sftp` included. `ts ssh` runs the OpenSSH client, which the `ts`
wrapper carries on its own `PATH`, so it works with the OpenSSH module off.
`agentix sandbox shell` over Tailscale does not: it checks the host keys,
which the sandbox reads with `ssh-keygen`, so with the OpenSSH module off it
finds none and refuses.
An `scp -O` to the sandbox, which runs `scp` there instead of SFTP, needs the
OpenSSH module.

With the OpenSSH module off, `sandbox ssh start --engine=openssh` fails with
"This sandbox's image has no OpenSSH: agentix.sandbox.openssh is off in its
Home Manager profile." before it asks for a key or writes anything, and
`status` and `stop` report a stopped listener. With the Tailscale module
off, the image has no `ts`, so `sandbox ssh start` with the Tailscale engine
and `sandbox up --tailscale` fail.

The image build checks OpenSSH only when the manifest lists `sshd`, and
Rex's ssh guard only when it lists `ssh`. `nix flake check` fails if the
sandbox profile stops listing them, or if a module that is off still
installs its package, lists its commands, or adds its account.

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
and terminal, but nothing restarts it after a crash or in a restored sandbox,
which starts with no processes. Run the same command again to restore it. From
the host, `agentix sandbox ssh start` starts it again, and `sandbox shell` does
when Tailscale is not running.
Existing authenticated state does not require 1Password to be available.

State defaults to `$HOME/.local/state/agentix/tailscale`, under `/root`, so a
snapshot of a Modal sandbox holds it; see the end of this section. Where
nothing keeps that directory, as in a container that is replaced, mount a
persistent volume and use the same path on each start, for example:

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

On Modal, a name keeps its Tailscale identity through `agentix sandbox
restore`, with no volume. Nothing starts Tailscale when a sandbox boots:
`sandbox restore --tailscale`, `sandbox ssh start`, or `sandbox shell` starts
it from the state in the snapshot, under a hostname that defaults to the
sandbox's name, or to its lowercase ID for a sandbox without one. All three
run `src/agentix/scripts/tailscale-ssh.sh`. If the control server has removed
the old node meanwhile, as it removes an ephemeral node some time after it
went offline, the node stays offline and is asked to log in. The script then
logs out, which drops only the node key, and enrolls again with `ts up` under
the same hostname, which reads the auth key from 1Password again. The node
gets a new address and keeps its SSH host keys.

If Tailscale names the node `NAME-1` because another node still has `NAME`,
the script renames it away and back, which takes `NAME` if it is free by now;
otherwise `start` adds a `warning`. See [Tailscale](../README.md#tailscale)
in the project README. A sandbox with no snapshot to restore, such as one
started with `--ephemeral` or under a name that was purged, enrolls as a new
node.

## Rex in sandboxes

`modules/sandbox/rex.nix` sets up Rex with the Home Manager module from Rex's
own flake, as on the nixos box: the `rex` command, the `xterm-rex` terminfo
entry in `~/.terminfo`, and Rex's ssh guard in `~/.ssh/config`, which Home
Manager now writes for root. The flake input is one build from the snapshot
channel, and follows this flake's nixpkgs. The flake and the release binary
download without credentials, so the image build needs no secret. The Mac
profile leaves the module off; the Rex app installs `rex` and runs the server
there.

The input names a snapshot by its versioned URL,
`https://pkgs.superlogical.com/rex/nix/snapshot/VERSION.tar.gz`, which always
serves the same files, so `nix flake update` leaves it where it is. The
channel's URL, `snapshot.tar.gz`, serves the newest snapshot, but Nix locks it
as the versioned URL that the server's `Link` header names, and that header
has named an older snapshot than the one served: the lock then paired one
snapshot's URL with another's hash, and every evaluation failed with a
`narHash` mismatch. To move to the newest snapshot:

```sh
uv run agentix flake update rex     # with other names, those move as well
uv run agentix image build          # then verify, as AGENTS.md says
```

It reads the version from `sources.json` in the channel's tarball, checks that
the versioned tarball holds the same version, and points the `rex` URL in
`flake.nix` at it. It writes `flake.nix` and `flake.lock` only when Nix locked
exactly that URL and the `modal` profile evaluates. If the versioned tarball is
not published yet, it stops and changes nothing; try again later. Old
versioned tarballs have stayed available so far: in November 2026, snapshot
and preview tarballs from September, and their release binaries, still
downloaded. The weekly
update job does not move Rex. The same works for the preview channel: change `snapshot/VERSION` in the
URL to a preview version, such as `preview/0.1.0-preview.19`. In November
2026 the stable channel served nothing yet.

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
