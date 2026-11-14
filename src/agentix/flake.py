"""Run `nix flake` on the Nix configuration in a sandbox and bring the lock back.

The host needs no Nix: the sandbox has it, and only `flake.nix` and
`flake.lock` travel.
"""

import json
from pathlib import Path

import modal

from .errors import OperationFailed

NIX_DIR = Path(__file__).resolve().parents[2] / "nix"
# A directory of its own, so files already in the image are not picked up.
_FLAKE_DIR = "/tmp/agentix-flake"


def lock_in_sandbox(app: modal.App, image: modal.Image, *command: str) -> str:
    """Run `nix flake COMMAND` on the host's flake and return the new lock file."""
    lockfile = NIX_DIR / "flake.lock"
    sandbox = modal.Sandbox.create(app=app, image=image, runtime="vm", timeout=600)
    try:
        sandbox.filesystem.make_directory(_FLAKE_DIR)
        sandbox.filesystem.copy_from_local(
            NIX_DIR / "flake.nix", f"{_FLAKE_DIR}/flake.nix"
        )
        if lockfile.exists():
            sandbox.filesystem.copy_from_local(lockfile, f"{_FLAKE_DIR}/flake.lock")
        # Nix reads the flake in the current directory.
        process = sandbox.exec(
            "nix", "flake", *command, workdir=_FLAKE_DIR, timeout=300
        )
        output = process.stdout.read()
        errors = process.stderr.read()
        if process.wait() != 0:
            raise OperationFailed(
                f"nix flake {' '.join(command)} failed: {errors}\n{output}"
            )
        return sandbox.filesystem.read_text(f"{_FLAKE_DIR}/flake.lock")
    finally:
        sandbox.terminate(wait=True)


def _revisions(lock_text: str) -> dict[str, str]:
    nodes = json.loads(lock_text)["nodes"]
    return {
        name: node["locked"].get("rev", node["locked"].get("narHash", ""))
        for name, node in nodes.items()
        if "locked" in node
    }


def update(app: modal.App, image_name: str, inputs: tuple[str, ...]) -> dict:
    """Move the named inputs, or all of them, to their newest revisions.

    Writes `nix/flake.lock` on the host and returns the inputs that moved.
    """
    lockfile = NIX_DIR / "flake.lock"
    before = _revisions(lockfile.read_text()) if lockfile.exists() else {}
    unknown = sorted(set(inputs) - set(before)) if before else []
    if unknown:
        raise OperationFailed(
            f"Not in flake.lock: {', '.join(unknown)}. "
            f"Inputs: {', '.join(sorted(before))}."
        )
    lock_text = lock_in_sandbox(
        app, modal.Image.from_name(image_name), "update", *inputs
    )
    after = _revisions(lock_text)
    changed = {
        name: {"from": before.get(name, ""), "to": revision}
        for name, revision in sorted(after.items())
        if before.get(name) != revision
    }
    if changed:
        lockfile.write_text(lock_text)
    return {"lockfile": str(lockfile), "changed": changed}
