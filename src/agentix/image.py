from pathlib import Path

import modal

from .shared import get_app

IMAGE_NAME = "determinate-nix:latest"
NIX_DIR = Path(__file__).resolve().parents[2] / "nix"
HOME_CONFIG = "/root/.config/home-manager"
GENERATION = "/opt/agentix/home-generation"
# A pinned release, so a rebuild months later installs the same Nix.
NIX_INSTALLER = "https://install.determinate.systems/nix/tag/v3.24.2"
# Default working directory of sandboxes. The Home Manager profile marks this
# path as trusted for Claude; keep nix/home-manager/modules/sandbox/claude.nix
# in step.
WORKDIR = "/workdir"
# Commands the image must provide. The build fails if one is missing.
REQUIRED_COMMANDS = (
    "claude gh gh-lf op ts herdr nvim home-manager direnv "
    "git rg fd fzf jq yq ast-grep sqlite3 curl rsync dig zip unzip zstd make just "
    "python3 uv bun node ruff ty shellcheck shfmt nixfmt statix deadnix "
    "typescript-language-server bash-language-server nixd ps strace"
)


def _ensure_lockfile(image: modal.Image):
    """Use remote Nix for the initial lock; subsequent builds use the local pin."""
    lockfile = NIX_DIR / "flake.lock"
    if lockfile.exists():
        return
    image = image.build(app=get_app())
    sandbox = modal.Sandbox.create(
        app=get_app(), image=image, runtime="vm", timeout=600
    )
    try:
        sandbox.filesystem.make_directory(HOME_CONFIG)
        sandbox.filesystem.copy_from_local(
            NIX_DIR / "flake.nix", f"{HOME_CONFIG}/flake.nix"
        )
        process = sandbox.exec("nix", "flake", "lock", HOME_CONFIG, timeout=300)
        output = process.stdout.read()
        errors = process.stderr.read()
        if process.wait() != 0:
            raise RuntimeError(f"Could not lock Nix inputs: {errors}\n{output}")
        lockfile.write_text(sandbox.filesystem.read_text(f"{HOME_CONFIG}/flake.lock"))
    finally:
        sandbox.terminate(wait=True)


def build_image(force_build: bool = False, image_name: str = IMAGE_NAME):
    with modal.enable_output():
        image = modal.Image.debian_slim("3.14", force_build=force_build)
        image = image.env({"DEBIAN_FRONTEND": "noninteractive", "HOME": "/root"})
        image = image.apt_install("curl", "git", "coreutils", "gnupg")
        # Modal containers have no systemd to manage the Nix daemon.
        image = image.run_commands(
            f"curl -fsSL {NIX_INSTALLER} | "
            "sh -s -- install linux --no-confirm --init none "
            '--extra-conf "sandbox = false"'
        )
        # Build steps and Sandbox.exec do not source the installer's shell profile.
        image = image.dockerfile_commands(
            'ENV PATH="/nix/var/nix/profiles/default/bin:${PATH}"',
            'ENV NIX_REMOTE="local"',
        )
        image = image.run_commands("nix --version")
        _ensure_lockfile(image)
        image = image.add_local_dir(NIX_DIR, HOME_CONFIG, copy=True)
        image = image.env({"USER": "root", "LOGNAME": "root"})
        # One layer, because files deleted by a later layer stay in the image.
        # The collection removes the build inputs and sources, which are about
        # half of what the build downloads. Evaluation is its own Nix process
        # so the build starts without the evaluator's memory. `eval-cores` is
        # set for that command only and does not reach sandboxes.
        image = image.run_commands(
            "mkdir -p /opt/agentix "
            "&& drv=$(NIX_CONFIG='eval-cores = 1' nix eval --no-write-lock-file --raw "
            f"{HOME_CONFIG}#homeConfigurations.modal.activationPackage.drvPath) "
            "&& nix-store --realise --max-jobs 2 --cores 2 "
            f'--add-root {GENERATION} "$drv" '
            f"&& nix profile add {GENERATION}/home-path "
            f"&& {GENERATION}/activate "
            "&& nix store gc "
            "&& rm -rf /root/.cache/nix "
            "&& du -sh /nix/store"
        )
        # Sandbox.exec does not start a login shell, so it needs these here.
        # SSH logins get the same values from Home Manager's session variables.
        image = image.dockerfile_commands(
            'ENV PATH="/root/.nix-profile/bin:${PATH}"',
            'ENV EDITOR="nvim" VISUAL="nvim" LANG="C.UTF-8"',
            'ENV NIX_PATH="nixpkgs=flake:nixpkgs"',
        )
        image = image.run_commands(f"mkdir -p {WORKDIR}")
        image = image.workdir(WORKDIR)
        # SSH sessions start in $HOME; this moves them to the working directory.
        # Login shells read it from /etc/profile.d, and .bashrc reads it for
        # `ssh HOST COMMAND`.
        image = image.add_local_file(
            Path(__file__).with_name("scripts") / "workdir-profile.sh",
            "/etc/profile.d/agentix-workdir.sh",
            copy=True,
        )
        image = image.run_commands(
            f'test "$PWD" = {WORKDIR}',
            f"for name in {REQUIRED_COMMANDS}; do "
            'command -v "$name" > /dev/null || { echo "missing: $name"; exit 1; }; '
            "done",
            "home-manager --version",
            "nvim --headless '+lua assert(vim.lsp.config.ty)' +qa",
            "op --version && git --version && uv --version && bun --version",
            # The evaluation setting must not leak into sandboxes.
            'test -z "${NIX_CONFIG:-}"',
            'test "$(locale charmap)" = UTF-8',
            # A login shell started inside an SSH command keeps the Nix profile.
            # A plain SSH command gets the session variables from .bashrc and
            # starts in the working directory.
            "env __ETC_PROFILE_NIX_SOURCED=1 bash -lc 'command -v claude' > /dev/null",
            "env -u LANG bash -c '. /root/.bashrc; test \"$LANG\" = C.UTF-8'",
            "env SSH_CONNECTION=check bash -c "
            f"'cd /root && . /root/.bashrc; test \"$PWD\" = {WORKDIR}'",
            "test -s /root/.claude/CLAUDE.md",
            "test -s /root/.claude/skills/claude-code-home-manager/.lsp.json",
            "jq -e '.permissions.deny | index(\"Read(//run/agentix/op-token)\")' "
            "/root/.claude/settings.json > /dev/null",
            "nix registry list | grep -q '^user  *flake:nixpkgs '",
        )
        image = image.build(app=get_app())
        image.publish(name=image_name)
        return image
