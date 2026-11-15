# Sandbox environment

You are root in a disposable Modal sandbox: a Debian VM with no systemd and no
Nix daemon. Work in `@workdir@` unless told otherwise. Files are lost when the
sandbox ends, so push or hand back anything that must survive.

## Tools

- Every tool on PATH comes from one pinned nixpkgs revision through Home Manager.
- Need a tool that is missing? Run it without installing:
  `nix run nixpkgs#NAME -- ARGS` or `nix shell nixpkgs#NAME -c COMMAND`.
  `nixpkgs` resolves to the image's revision, so most packages download from the
  binary cache in seconds. Find names with `nix search nixpkgs TERM`.
- There is no `sudo`; you are already root. Prefer Nix to `apt-get`: packages
  installed with apt are invisible to Nix-built tools and are not reproducible.
- There is no C compiler. Build native code inside
  `nix shell nixpkgs#gcc -c COMMAND`, or in a project's own Nix shell.
- Python: use `uv` (`uv run`, `uv sync`, `uv tool run`); do not `pip install`
  into the system interpreter. JavaScript and TypeScript: `bun` and `node`.
- Installed for checks: `ruff`, `ty`, `shellcheck`, `shfmt`, `nixfmt`, `statix`,
  `deadnix`, `ast-grep`. Language servers for Python, TypeScript, Bash, and Nix
  back the LSP tool.
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

## Changing this environment

- The configuration lives in the agentix repository under `nix/`; a copy is in
  `/root/.config/home-manager`. `home-manager switch --flake
  /root/.config/home-manager#modal` changes this sandbox only. The first switch
  downloads build inputs and takes a few minutes.
- A lasting change is a commit to that repository followed by an image rebuild.
