from pathlib import Path

import modal

from .shared import get_app

IMAGE_NAME = "determinate-nix:latest"
NIX_DIR = Path(__file__).resolve().parents[2] / "nix"
HOME_CONFIG = "/root/.config/home-manager"
# Default working directory of sandboxes. The Home Manager profile marks this
# path as trusted for Claude; keep nix/home-manager/modules/claude-code.nix in sync.
WORKDIR = "/workdir"


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
        sandbox.filesystem.copy_from_local(NIX_DIR / "flake.nix", f"{HOME_CONFIG}/flake.nix")
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
            "curl -fsSL https://install.determinate.systems/nix | "
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
        # Evaluate separately so build workers start from a fresh Nix process.
        # Cache the evaluated inputs before the larger package-build layer.
        image = image.env({"NIX_CONFIG": "eval-cores = 1"})
        image = image.run_commands(
            f"nix eval --no-write-lock-file --raw "
            f"{HOME_CONFIG}#homeConfigurations.modal.activationPackage.drvPath "
            "> /tmp/agentix-home.drv",
        )
        image = image.run_commands(
            "mkdir -p /opt/agentix && nix-store --realise --max-jobs 2 --cores 2 "
            '--add-root /opt/agentix/home-generation "$(cat /tmp/agentix-home.drv)"'
        )
        image = image.env({"USER": "root", "LOGNAME": "root"})
        image = image.run_commands(
            "nix profile add /opt/agentix/home-generation/home-path "
            "&& /opt/agentix/home-generation/activate"
        )
        image = image.dockerfile_commands(
            'ENV PATH="/root/.nix-profile/bin:${PATH}"',
            'ENV EDITOR="nvim" VISUAL="nvim"',
        )
        image = image.run_commands(f"mkdir -p {WORKDIR}")
        image = image.workdir(WORKDIR)
        # SSH login shells start in $HOME; this moves them to the working directory.
        image = image.add_local_file(
            Path(__file__).with_name("scripts") / "workdir-profile.sh",
            "/etc/profile.d/agentix-workdir.sh",
            copy=True,
        )
        image = image.run_commands(
            f'test "$PWD" = {WORKDIR}',
            "home-manager --version",
            "nvim --headless '+lua assert(vim.lsp.config.ty)' +qa",
            "op --version && git --version && uv --version && bun --version",
        )
        image = image.build(app=get_app())
        image.publish(name=image_name)
        return image
