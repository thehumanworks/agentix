import functools
import json
import shlex

import modal

from .defaults import APP_NAME, IMAGE_NAME
from .flake import lock_in_sandbox
from .layout import MANIFEST, NIX_DIR, OP_TOKEN_FILE, PATHS, WORKDIR

HOME_CONFIG = "/root/.config/home-manager"
GENERATION = "/opt/agentix/home-generation"
# The manifest of the generation the image activates. It lists the commands
# the Home Manager configuration provides and the paths it shares with the
# host; see nix/sandbox.toml.
MANIFEST_FILE = f"{GENERATION}/home-path/{MANIFEST}"

# What the build uses before Nix exists, pinned so that two builds from one
# flake.lock start from the same files; everything after Nix follows the lock.
#
# The base image, by digest: Debian 12 (bookworm) of 2026-11-04 with the curl
# and CA certificates that the Nix installer needs to download itself, so the
# build runs no apt. To move it, look up the tag's current digest, for example
# in a sandbox with `nix run nixpkgs#crane -- digest buildpack-deps:bookworm-curl`,
# replace the digest here, and rebuild.
BASE_IMAGE = (
    "buildpack-deps:bookworm-curl"
    "@sha256:58021c2a0204361b62a23d2e0244d144d74c29af2d678e2a1f7e6611885d6949"
)
# The Nix installer: a release, checked against its digest, so a rebuild
# months later installs the same Nix. To move it, take a version and the
# SHA-256 of its nix-installer-x86_64-linux from
# https://github.com/DeterminateSystems/nix-installer/releases.
NIX_INSTALLER_VERSION = "v3.24.2"
NIX_INSTALLER_SHA256 = (
    "22ed14ee1b24d100d388f1fc245e3062bd5982433d159cffa6dec34c0eb15dab"
)
NIX_INSTALLER_URL = (
    "https://install.determinate.systems/nix/tag/"
    f"{NIX_INSTALLER_VERSION}/nix-installer-x86_64-linux"
)


@functools.cache
def get_app() -> modal.App:
    return modal.App.lookup(name=APP_NAME, create_if_missing=True)


def _ensure_lockfile(image: modal.Image):
    """Use remote Nix for the initial lock; subsequent builds use the local pin."""
    lockfile = NIX_DIR / "flake.lock"
    if lockfile.exists():
        return
    image = image.build(app=get_app())
    lockfile.write_text(lock_in_sandbox(get_app(), image, "lock"))


def build_image(force_build: bool = False, image_name: str = IMAGE_NAME):
    with modal.enable_output():
        # A sandbox runs no Python of the image's, so the base needs none.
        image = modal.Image.from_registry(BASE_IMAGE, force_build=force_build)
        image = image.env({"DEBIAN_FRONTEND": "noninteractive", "HOME": "/root"})
        # Modal containers have no systemd to manage the Nix daemon. One layer,
        # so the installer does not stay in the image.
        image = image.run_commands(
            f"curl -fsSL --retry 3 -o /tmp/nix-installer {NIX_INSTALLER_URL} "
            f"&& echo '{NIX_INSTALLER_SHA256}  /tmp/nix-installer' | sha256sum -c - "
            "&& chmod +x /tmp/nix-installer "
            "&& /tmp/nix-installer install linux --no-confirm --init none "
            '--extra-conf "sandbox = false" '
            "&& rm /tmp/nix-installer"
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
        # The account sshd runs as, so `agentix sandbox ssh start
        # --engine=openssh` needs no useradd in a sandbox.
        image = image.run_commands(
            f"mkdir -p {WORKDIR}",
            "useradd --system --no-create-home --home-dir /run/sshd "
            "--shell /bin/false sshd",
        )
        image = image.workdir(WORKDIR)
        image = image.run_commands(*_checks())
        image = image.build(app=get_app())
        image.publish(name=image_name)
        return image


def _checks() -> list[str]:
    """Commands that fail the build when the image is not as expected."""
    deny_token_read = f"Read(/{OP_TOKEN_FILE})"
    return [
        f'test "$PWD" = {WORKDIR}',
        # The configuration read the same nix/sandbox.toml as this command, and
        # lists the commands it provides.
        f"jq -e --argjson paths {shlex.quote(json.dumps(PATHS))} "
        f"'.paths == $paths and (.commands | length > 0)' {MANIFEST_FILE} > /dev/null",
        f"test -s /root/.nix-profile/{MANIFEST}",
        # Each listed command resolves to the Nix profile, not to a copy in
        # the base image.
        f"commands=$(jq -er '.commands | join(\" \")' {MANIFEST_FILE}) && "
        'for name in $commands; do test "$(command -v "$name")" = '
        '"/root/.nix-profile/bin/$name" '
        '|| { echo "not in the Nix profile: $name"; exit 1; }; done',
        "id sshd > /dev/null",
        "home-manager --version",
        "nvim --headless '+lua assert(vim.lsp.config.ty)' +qa",
        "op --version && git --version && uv --version && bun --version",
        # The evaluation setting must not leak into sandboxes.
        'test -z "${NIX_CONFIG:-}"',
        'test "$(locale charmap)" = UTF-8',
        # A login shell started inside an SSH command keeps the Nix profile.
        # A plain SSH command gets the session variables from .bashrc. Both,
        # and a login, start in the working directory.
        "env __ETC_PROFILE_NIX_SOURCED=1 bash -lc 'command -v claude' > /dev/null",
        "env -u LANG bash -c '. /root/.bashrc; test \"$LANG\" = C.UTF-8'",
        "env SSH_CONNECTION=check bash -c "
        f"'cd /root && . /root/.bashrc; test \"$PWD\" = {WORKDIR}'",
        f"cd /root && env SSH_CONNECTION=check bash -lc 'test \"$PWD\" = {WORKDIR}'",
        "test -s /root/.claude/CLAUDE.md",
        "test -s /root/.claude/skills/claude-code-home-manager/.lsp.json",
        f"jq -e --arg rule {shlex.quote(deny_token_read)} "
        "'.permissions.deny | index($rule)' /root/.claude/settings.json > /dev/null",
        # The pinned plugin marketplace must survive the store collection.
        'test -s "$(jq -r \'.extraKnownMarketplaces["claude-mods"].source.path\' '
        '/root/.claude/settings.json)/.claude-plugin/marketplace.json"',
        "jq -e '.[\"claude-mods\"].lastUpdated' "
        "/root/.claude/plugins/known_marketplaces.json > /dev/null",
        "nix registry list | grep -q '^user  *flake:nixpkgs '",
        # The patched rex binary starts, its terminfo entry resolves, and
        # the ssh guard is in ~/.ssh/config. None of these starts a server.
        "rex version > /dev/null",
        "infocmp xterm-rex > /dev/null",
        "env TERM=xterm-rex ssh -G example.invalid "
        "| grep -qx 'setenv TERM=xterm-256color'",
        # Each sandbox's rex server makes its own state, including its host
        # ID; none may be baked into the image.
        "test ! -e /root/.local/state/rex",
    ]
