"""Build the sandbox image from the flake's OCI image and publish it on Modal.

`nix build .#oci` in nix/ defines the files of the image; see nix/oci/. Modal's
image builder cannot run that build: the Home Manager activation in it runs
under proot, which crashes there, and a stock Nix loses track of some of its
builds there. A VM sandbox builds it instead and unpacks its layers into a
directory. A snapshot of that directory is the root of the image, to which the
build adds the environment and working directory from nix/sandbox.toml, as the
OCI image has them. No registry and no credential is involved.
"""

import functools
import hashlib
import io
import json
import shlex
import sys
import tarfile

import modal

from .defaults import APP_NAME, IMAGE_NAME
from .errors import OperationFailed
from .flake import lock_in_sandbox
from .layout import IMAGE_ENV, MANIFEST, NIX_DIR, PATHS, WORKDIR

GENERATION = "/opt/agentix/home-generation"
# The manifest of the generation the image activates. It lists the commands
# the Home Manager configuration provides and the paths it shares with the
# host; see nix/sandbox.toml.
MANIFEST_FILE = f"{GENERATION}/home-path/{MANIFEST}"

# The Nix that builds the image. It runs the build and none of its files reach
# the image, whose Nix comes from flake.lock. This is the official image of a
# Nix release, of the series that the locked nixpkgs ships, pinned by digest.
# To move it, take a release's linux/amd64 digest, for example in a sandbox with
# `nix run nixpkgs#crane -- digest nixos/nix:VERSION-amd64`.
NIX_BUILDER = (
    "nixos/nix:2.35.6-amd64"
    "@sha256:a3b3e3eb3883e5af5327757c5304c0093426a0d11c58a9056df46cfefe30319e"
)
# The build sandbox. The build is mostly downloads and many small builds; with
# Modal's default share of a core it takes much longer.
BUILD_CPU = 8.0
BUILD_MEMORY_MIB = 16384
BUILD_TIMEOUT = 3600

# Where the build sandbox puts the flake and the unpacked image.
_SOURCE = "/tmp/agentix-source"
_ROOTFS = "/tmp/agentix-rootfs"
# Builds the OCI image and unpacks its layers, lowest first, into _ROOTFS. The
# layers hold distinct store paths and the files outside the store come last,
# so no layer removes what another adds.
_BUILD = f"""set -euo pipefail
exec 2>&1
mkdir -p {_SOURCE} {_ROOTFS}
tar -xf {_SOURCE}.tar -C {_SOURCE}
nix build --no-write-lock-file --out-link /tmp/agentix-oci \\
  path:{_SOURCE}#packages.x86_64-linux.oci
/tmp/agentix-oci | tar -x --wildcards '*/layer.tar' \\
  --to-command='tar -x -p -f - -C {_ROOTFS}'
du -sh {_ROOTFS}
"""
# The unpacked images, by the hash of what went into them, so that a build
# from the same sources and recipe reuses one; see _cache_key. Each is about
# 2 GB, and Modal keeps it until it is deleted.
ROOTFS_CACHE = "agentix-rootfs"


@functools.cache
def get_app() -> modal.App:
    return modal.App.lookup(name=APP_NAME, create_if_missing=True)


def builder_image() -> modal.Image:
    """The pinned Nix, with flakes on, for the build and for `nix flake lock`."""
    return modal.Image.from_registry(NIX_BUILDER).env(
        {"NIX_CONFIG": "experimental-features = nix-command flakes"}
    )


def _ensure_lockfile():
    """Use remote Nix for the initial lock; subsequent builds use the local pin."""
    lockfile = NIX_DIR / "flake.lock"
    if lockfile.exists():
        return
    lockfile.write_text(lock_in_sandbox(get_app(), builder_image(), "lock"))


def _source_archive() -> bytes:
    """nix/ as a tar archive with fixed metadata, so that its hash follows the files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(NIX_DIR.rglob("*")):
            info = archive.gettarinfo(path, path.relative_to(NIX_DIR).as_posix())
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.isreg():
                info.mode = 0o755 if info.mode & 0o100 else 0o644
                with path.open("rb") as file:
                    archive.addfile(info, file)
            elif info.isdir():
                info.mode = 0o755
                archive.addfile(info)
            elif info.issym():
                archive.addfile(info)
    return buffer.getvalue()


def _cache_key(source: bytes) -> str:
    """What decides the unpacked image: the sources, the builder, and the recipe."""
    digest = hashlib.sha256()
    for part in (source, NIX_BUILDER.encode(), _BUILD.encode()):
        digest.update(hashlib.sha256(part).digest())
    return digest.hexdigest()[:32]


def _find_rootfs(name: str) -> modal.Image | None:
    try:
        # A named image builds by looking up its ID.
        return modal.Image.from_name(name).build(app=get_app())
    except modal.exception.NotFoundError:
        return None


def _build_rootfs(source: bytes) -> modal.Image:
    """Build the OCI image in a sandbox and return a snapshot of its files."""
    sandbox = modal.Sandbox.create(
        app=get_app(),
        image=builder_image(),
        runtime="vm",
        cpu=BUILD_CPU,
        memory=BUILD_MEMORY_MIB,
        timeout=BUILD_TIMEOUT,
    )
    try:
        sandbox.filesystem.write_bytes(source, f"{_SOURCE}.tar")
        process = sandbox.exec("bash", "-c", _BUILD, timeout=BUILD_TIMEOUT - 300)
        for line in process.stdout:
            print(line, end="", file=sys.stderr, flush=True)
        if process.wait() != 0:
            raise OperationFailed(f"The image build failed in {sandbox.object_id}.")
        # Kept until deleted: the published image is built on it.
        return sandbox.snapshot_directory(_ROOTFS, timeout=900, ttl=None)
    finally:
        sandbox.terminate()


def build_image(force_build: bool = False, image_name: str = IMAGE_NAME):
    _ensure_lockfile()
    source = _source_archive()
    cached = f"{ROOTFS_CACHE}:{_cache_key(source)}"
    rootfs = None if force_build else _find_rootfs(cached)
    image = None
    if rootfs is not None:
        try:
            image = _finish(rootfs, force_build)
        except modal.exception.NotFoundError:
            # A root deleted with image_delete keeps its name, and Modal
            # refuses to build on it.
            print(f"{cached} was deleted; building it again.", file=sys.stderr)
    if image is None:
        rootfs = _build_rootfs(source)
        rootfs.publish(name=cached)
        image = _finish(rootfs, force_build)
    image.publish(name=image_name)
    return image


def _finish(rootfs: modal.Image, force_build: bool) -> modal.Image:
    """The image on ROOTFS, built and checked but not published."""
    with modal.enable_output():
        # The OCI image's entry point does what the start command of a sandbox
        # does (see sandbox.py), and Modal would run that command through it,
        # so leave it out. Without a command, such as for `flake update`, a
        # sandbox stays up, as the entry point keeps a container up.
        image = (
            modal.Image.from_id(rootfs.object_id)
            .env(IMAGE_ENV)
            .workdir(WORKDIR)
            .cmd(["sleep", "infinity"])
        )
        # The checks run in a layer that is not published, so the image holds
        # the OCI image's files and nothing that the checks leave behind.
        image.run_commands(*_checks(), force_build=force_build).build(app=get_app())
        image.build(app=get_app())
    return image


_HTTPS_CHECK = "https://cache.nixos.org/nix-cache-info"


def _if_listed(command: str, check: str) -> str:
    """CHECK, run when the manifest lists COMMAND, whose module may be off.

    `nix flake check` fails if the modal profile stops listing it.
    """
    return (
        f"if jq -e --arg name {shlex.quote(command)} '.commands | index($name)' "
        f"{MANIFEST_FILE} > /dev/null; then {check}; fi"
    )


def _checks() -> list[str]:
    """Commands that fail the build when the image does not work on Modal.

    The OCI build checks the files already: every command in the manifest is
    in root's profile, the activation ran, and Claude's deny rule names the
    token file. These check what Modal's copy does when run.
    """
    return [
        f'test "$PWD" = {WORKDIR}',
        # The snapshot kept modes, owners, links, and the Nix database.
        'test "$(stat -c %a:%u /root /etc/shadow /var/empty)" = "$(printf "700:0\\n600:0\\n755:0")"',
        "test -L /bin/sh",
        "nix path-info --recursive /root/.nix-profile > /dev/null",
        # HTTPS works with the certificate directory that Modal names in the
        # environment of sandbox commands, and without either variable, as in
        # SSH sessions.
        f"SSL_CERT_DIR=/etc/ssl/certs curl -fsS -o /dev/null {_HTTPS_CHECK}",
        f"env -u SSL_CERT_DIR -u SSL_CERT_FILE curl -fsS -o /dev/null {_HTTPS_CHECK}",
        "SSL_CERT_DIR=/etc/ssl/certs git ls-remote https://github.com/NixOS/nix HEAD",
        "SSL_CERT_DIR=/etc/ssl/certs python3 -c "
        f"'import urllib.request as r; r.urlopen(\"{_HTTPS_CHECK}\")'",
        # The configuration read the same nix/sandbox.toml as this command, and
        # lists the commands it provides.
        f"jq -e --argjson paths {shlex.quote(json.dumps(PATHS))} "
        f"'.paths == $paths and (.commands | length > 0)' {MANIFEST_FILE} > /dev/null",
        f"test -s /root/.nix-profile/{MANIFEST}",
        # The image's PATH finds each listed command in the Nix profile.
        f"commands=$(jq -er '.commands | join(\" \")' {MANIFEST_FILE}) && "
        'for name in $commands; do test "$(command -v "$name")" = '
        '"/root/.nix-profile/bin/$name" '
        '|| { echo "not in the Nix profile: $name"; exit 1; }; done',
        # With the OpenSSH module on, sshd accepts a configuration, which it
        # refuses without the sshd account or its directory, /var/empty. The
        # host key stays in this layer, which is not published.
        _if_listed(
            "sshd",
            "ssh-keygen -q -t ed25519 -N '' -f /tmp/agentix-check-key && "
            '"$(command -v sshd)" -t -f /dev/null -h /tmp/agentix-check-key',
        ),
        "home-manager --version",
        "nvim --headless '+lua assert(vim.lsp.config.ty)' +qa",
        "op --version && git --version && uv --version && bun --version",
        'test "$(locale charmap)" = UTF-8',
        # A login shell started inside an SSH command keeps the Nix profile.
        # A plain SSH command gets the session variables from .bashrc. Both,
        # and a login, start in the working directory.
        "env -u PATH /bin/bash -lc 'command -v claude' > /dev/null",
        "env -u LANG bash -c '. /root/.bashrc; test \"$LANG\" = C.UTF-8'",
        "env SSH_CONNECTION=check bash -c "
        f"'cd /root && . /root/.bashrc; test \"$PWD\" = {WORKDIR}'",
        f"cd /root && env SSH_CONNECTION=check bash -lc 'test \"$PWD\" = {WORKDIR}'",
        "test -s /root/.claude/skills/claude-code-home-manager/.lsp.json",
        "jq -e '.[\"claude-mods\"].lastUpdated' "
        "/root/.claude/plugins/known_marketplaces.json > /dev/null",
        "nix registry list | grep '^user  *flake:nixpkgs ' > /dev/null",
        # The patched rex binary starts, its terminfo entry resolves, and
        # the ssh guard is in ~/.ssh/config, for the client that the OpenSSH
        # module installs. None of these starts a server.
        "rex version > /dev/null",
        "infocmp xterm-rex > /dev/null",
        _if_listed(
            "ssh",
            "env TERM=xterm-rex ssh -G example.invalid "
            "| grep -qx 'setenv TERM=xterm-256color'",
        ),
        # Each sandbox's rex server makes its own state, including its host
        # ID; none may be baked into the image.
        "test ! -e /root/.local/state/rex",
        # The compiler builds and links a program against glibc on its own.
        _if_listed(
            "cc",
            "printf '#include <stdio.h>\\nint main(void) { puts(\"ok\"); }\\n' "
            "> /tmp/agentix-check.c && cc -o /tmp/agentix-check /tmp/agentix-check.c "
            '&& test "$(/tmp/agentix-check)" = ok',
        ),
        # The headless browser renders a page as root, and Playwright finds
        # the same build where the image's environment points it.
        _if_listed(
            "chrome-headless-shell",
            "chrome-headless-shell --dump-dom 'data:text/html,<title>agentix</title>' "
            "2> /dev/null | grep '<title>agentix</title>' > /dev/null && "
            'test -x "$(echo "$PLAYWRIGHT_BROWSERS_PATH"/chromium_headless_shell-*/'
            'chrome-headless-shell-*/chrome-headless-shell)"',
        ),
        # comma and nix-locate read the index in the image, without building
        # one. grep reads all of the output, so nix-locate does not panic on
        # a closed pipe.
        _if_listed(
            ",",
            "nix-locate --minimal --whole-name --at-root /bin/hello | grep -x hello.out > /dev/null",
        ),
    ]
