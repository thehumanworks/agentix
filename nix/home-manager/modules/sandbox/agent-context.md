# Sandbox environment

You are root in a disposable sandbox, normally a Modal VM. Every file in it
comes from Nix: there is no Debian or other distribution under it, no systemd,
and no Nix daemon. Work in `@workdir@` unless told otherwise. Files are lost
when the sandbox ends, so push or hand back anything that must survive.

## Tools

- Every tool on PATH comes from one pinned nixpkgs revision through Home Manager.
- Need a tool that is missing? `COMMA_CACHING=0 , COMMAND ARGS` (comma) finds
  the package that has COMMAND in a prebuilt index and runs it without
  installing it. When several packages have it and none is named COMMAND,
  comma lists them and fails; pick one with `nix shell`. `COMMA_CACHING=0`
  (or `--cache-level 0`) keeps comma from remembering the choice in
  `~/.cache`, which snapshots keep. `nix-locate bin/COMMAND` lists the
  candidates. Or name the package:
  `nix run nixpkgs#NAME -- ARGS` or `nix shell nixpkgs#NAME -c COMMAND`.
  `nixpkgs` resolves to the image's revision, so most packages download from the
  binary cache in seconds. Find names with `nix search nixpkgs TERM`.
- There is no `sudo`; you are already root. There is no `apt-get` either, and
  no `perl`, `gpg`, or `useradd` outside Nix: run them from nixpkgs, for
  example `nix run nixpkgs#gnupg -- ARGS`.
- `cc`/`gcc`/`g++` and binutils are installed, with `make` and `pkg-config`.
  Libraries that are not in the image come from a project's Nix shell or
  `nix shell nixpkgs#LIB.dev -c COMMAND`.
- Headless browser: `chrome-headless-shell --dump-dom URL`,
  `--screenshot=FILE --window-size=W,H URL`, or `--print-to-pdf=FILE URL`.
  Playwright uses the same build through `PLAYWRIGHT_BROWSERS_PATH` when the
  client is of the minor release in `$PLAYWRIGHT_BROWSERS_PATH/VERSION`
  (say 1.64): `bunx playwright@1.64 screenshot URL FILE`, a project's
  `@playwright/test` of that release, or Python from nixpkgs:
  `nix shell --impure --expr '(import <nixpkgs> {}).python3.withPackages (p: [ p.playwright ])' -c python3 SCRIPT`.
  The PyPI wheel under `uv` fails to load (its greenlet needs libstdc++), and
  other releases do not find a browser.
  The browser runs as root with `--no-sandbox`, so this sandbox is its only
  isolation: do not open untrusted sites while the sandbox holds tokens.
- Python: use `uv` (`uv run`, `uv sync`, `uv tool run`); do not `pip install`
  into the system interpreter. JavaScript and TypeScript: `bun` and `node`.
- Also installed: `psql`, `hyperfine` (benchmarks), `watchexec` (rerun on
  change), `difft` (structural diff), `nom` (`nom build` shows Nix progress).
- Installed for checks: `ruff`, `ty`, `shellcheck`, `shfmt`, `nixfmt`, `statix`,
  `deadnix`, `ast-grep`. Language servers for Python, TypeScript, Bash, and Nix
  are on PATH and back Claude's LSP tool.
- `rex` is the terminal multiplexer. `rex skill show` explains how to drive it;
  the first command starts its server.
- A project with an `.envrc` or `flake.nix`: run `direnv allow` once, then
  `direnv exec . COMMAND`. Your shell is not interactive, so the direnv hook
  does not load the environment for you.

## Secrets

- `op` is already authenticated. Read a secret at the point of use with
  `op run -- COMMAND` or `op read 'op://VAULT/ITEM/FIELD'`.
- `gh` (github.com) and `gh-lf` (work GitHub Enterprise) fetch their own tokens
  through `op`; `git` over HTTPS uses them as credential helpers.
- Credentials are removed from the environment of the commands you run. Do not
  print secret values or write them to files.
- The agent harnesses `claude`, `codex`, and `pi` are installed and signed in.
  Codex's `~/.codex/auth.json` and pi's `~/.pi/agent/auth.json` come from
  1Password, and `agent-auth-sync` pushes refreshed tokens back; leave those
  files to it. `agent-auth-sync status` shows whether they are in step.

## Changing this environment

- The configuration lives in the agentix repository under `nix/`; a copy is in
  `/root/.config/home-manager`. `home-manager switch --flake
  /root/.config/home-manager#modal` changes this sandbox only. The first switch
  downloads build inputs and takes a few minutes.
- A lasting change is a commit to that repository followed by an image rebuild.
