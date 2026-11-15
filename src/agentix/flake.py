"""Run `nix flake` on the Nix configuration in a sandbox and bring the lock back.

The host needs no Nix: the sandbox has it, and `nix/` travels there. An update
is written back only once the home profile evaluates with the new lock.
"""

import io
import json
import re
import tarfile
import urllib.request

import modal

from .errors import OperationFailed
from .layout import NIX_DIR

# A directory of its own, so files already in the image are not picked up.
_FLAKE_DIR = "/tmp/agentix-flake"
# The profile the image builds. Evaluating it fetches every input from the URL
# the lock records, so a lock whose URL and hash disagree fails here.
_PROFILE = "homeConfigurations.modal.activationPackage.drvPath"

# The rex input names one snapshot by its versioned URL, BASE/CHANNEL/VERSION
# .tar.gz, which never changes. BASE/CHANNEL.tar.gz serves the newest, but Nix
# locks that URL as whatever its Link header names, and the header has named
# an older snapshot than the one served.
_REX_INPUT = re.compile(
    r'url = "((https://pkgs\.superlogical\.com/rex/nix/[a-z]+)/([^"/]+)\.tar\.gz)";'
)
_REX_VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]*")


def lock_in_sandbox(
    app: modal.App,
    image: modal.Image,
    *command: str,
    flake_nix: str | None = None,
    check: bool = False,
) -> str:
    """Run `nix flake COMMAND` on the host's flake and return the new lock file.

    FLAKE_NIX replaces flake.nix in the copy. With CHECK, the home profile must
    evaluate with the new lock.
    """
    sandbox = modal.Sandbox.create(app=app, image=image, runtime="vm", timeout=600)
    try:
        # All of nix/, because evaluation reads the modules as well.
        sandbox.filesystem.make_directory(_FLAKE_DIR)
        sandbox.filesystem.write_bytes(_archive(), f"{_FLAKE_DIR}.tar")
        _run(sandbox, "tar", "-xf", f"{_FLAKE_DIR}.tar", "--no-same-owner")
        if flake_nix is not None:
            sandbox.filesystem.write_text(flake_nix, f"{_FLAKE_DIR}/flake.nix")
        _run(sandbox, "nix", "flake", *command)
        if check:
            _run(sandbox, "nix", "eval", "--raw", f".#{_PROFILE}")
        return sandbox.filesystem.read_text(f"{_FLAKE_DIR}/flake.lock")
    finally:
        sandbox.terminate(wait=True)


def _archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(NIX_DIR, arcname=".")
    return buffer.getvalue()


def _run(sandbox: modal.Sandbox, *command: str) -> None:
    # Nix reads the flake in the current directory.
    process = sandbox.exec(*command, workdir=_FLAKE_DIR, timeout=300)
    output = process.stdout.read()
    errors = process.stderr.read()
    if process.wait() != 0:
        raise OperationFailed(f"{' '.join(command)} failed: {errors}\n{output}")


def _revisions(lock_text: str) -> dict[str, str]:
    nodes = json.loads(lock_text)["nodes"]
    return {
        name: node["locked"].get("rev", node["locked"].get("narHash", ""))
        for name, node in nodes.items()
        if "locked" in node
    }


def _rex_version(url: str) -> str:
    """The Rex version that the flake tarball at URL records in sources.json."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            member = archive.extractfile("rex/sources.json") or io.BytesIO()
            sources = json.load(member)
    except (OSError, tarfile.TarError, KeyError, ValueError) as error:
        raise OperationFailed(f"no Rex version at {url}: {error}") from error
    version = sources.get("version") if isinstance(sources, dict) else None
    if not (isinstance(version, str) and _REX_VERSION.fullmatch(version)):
        raise OperationFailed(f"{url} names an unexpected Rex version: {version!r}")
    return version


def _newest_rex(flake_nix: str) -> tuple[str, dict[str, str]]:
    """Point the rex input at the newest snapshot that its channel serves."""
    found = list(_REX_INPUT.finditer(flake_nix))
    if len(found) != 1:
        raise OperationFailed(
            "flake.nix must name Rex once, as url = "
            '"https://pkgs.superlogical.com/rex/nix/CHANNEL/VERSION.tar.gz";'
        )
    (match,) = found
    version = _rex_version(f"{match[2]}.tar.gz")
    url = f"{match[2]}/{version}.tar.gz"
    # The versioned tarball is published beside the channel's and may lag.
    try:
        published = _rex_version(url)
    except OperationFailed as error:
        raise OperationFailed(
            f"The channel serves Rex {version}, but {error}. Nothing was changed; "
            "try again later."
        ) from error
    if published != version:
        raise OperationFailed(f"{url} holds Rex {published}, not {version}.")
    moved = flake_nix[: match.start(1)] + url + flake_nix[match.end(1) :]
    return moved, {"from": match[3], "to": version, "url": url}


def update(app: modal.App, image_name: str, inputs: tuple[str, ...]) -> dict:
    """Move the named inputs, or all of them, to their newest revisions.

    Rex moves only when named: its input is one snapshot, and naming it points
    nix/flake.nix at the newest. Writes nix/flake.lock, and flake.nix when Rex
    moved, once the home profile evaluates with the new lock, and returns the
    inputs that moved.
    """
    lockfile = NIX_DIR / "flake.lock"
    before = _revisions(lockfile.read_text()) if lockfile.exists() else {}
    unknown = sorted(set(inputs) - set(before)) if before else []
    if unknown:
        raise OperationFailed(
            f"Not in flake.lock: {', '.join(unknown)}. "
            f"Inputs: {', '.join(sorted(before))}."
        )
    flake_nix = rex = None
    if "rex" in inputs:
        flake_nix, rex = _newest_rex((NIX_DIR / "flake.nix").read_text())
    lock_text = lock_in_sandbox(
        app,
        modal.Image.from_name(image_name),
        "update",
        *inputs,
        flake_nix=flake_nix,
        check=True,
    )
    result: dict = {"lockfile": str(lockfile)}
    if rex:
        # Nix locks another URL when the server's Link header names one.
        node = json.loads(lock_text)["nodes"]["rex"]
        urls = sorted({str(node[key].get("url")) for key in ("original", "locked")})
        if urls != [rex["url"]]:
            raise OperationFailed(
                f"Nix locked Rex as {', '.join(urls)}, not {rex['url']}. "
                "Nothing was written."
            )
        if rex["from"] != rex["to"]:
            result["rex"] = {"from": rex["from"], "to": rex["to"]}
    after = _revisions(lock_text)
    result["changed"] = {
        name: {"from": before.get(name, ""), "to": revision}
        for name, revision in sorted(after.items())
        if before.get(name) != revision
    }
    if result["changed"] or "rex" in result:
        if flake_nix is not None:
            (NIX_DIR / "flake.nix").write_text(flake_nix)
        lockfile.write_text(lock_text)
    return result
