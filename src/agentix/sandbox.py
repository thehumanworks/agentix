import shlex

import modal

from .image import IMAGE_NAME
from .shared import get_app

# Modal gives the token only to the processes it starts. SSH sessions do not
# have it, so the image's `op` wrapper reads it from this root-only file.
# Keep the path in step with nix/home-manager/modules/sandbox/op-token.nix.
OP_TOKEN_FILE = "/run/agentix/op-token"
_STORE_OP_TOKEN = f"""set -eu
umask 077
mkdir -p "$(dirname {OP_TOKEN_FILE})"
printf %s "$OP_SERVICE_ACCOUNT_TOKEN" > {OP_TOKEN_FILE}
"""

WORKDIR_FILE = "/run/agentix/workdir"

# Modal's default reservation leaves a sandbox with about 1 GiB, which Nix
# evaluation, language servers, and test suites exhaust.
MEMORY_MIB = 8192
_STORE_WORKDIR = f"""set -eu
mkdir -p "$(dirname {WORKDIR_FILE})"
printf '%s\\n' "$1" > {WORKDIR_FILE}
"""


def create_sandbox(
    image_name: str = IMAGE_NAME,
    secret_name: str = "1password",
    timeout: int = 3600,
    workdir: str = "",
    memory: int = MEMORY_MIB,
    cpu: float = 0,
):
    """Start a sandbox in the image's working directory, or in `workdir` if given.

    `memory` is the reservation in MiB. `cpu` reserves that many cores; zero
    keeps Modal's default.
    """
    sb = modal.Sandbox.create(
        app=get_app(),
        image=modal.Image.from_name(image_name),
        env={"IS_SANDBOX": "1"},
        secrets=[
            modal.Secret.from_name(
                secret_name, required_keys=["OP_SERVICE_ACCOUNT_TOKEN"]
            )
        ]
        if secret_name
        else [],
        timeout=timeout,
        workdir=workdir or None,
        memory=memory,
        cpu=cpu or None,
        runtime="vm",
        unencrypted_ports=[22],
    )
    if secret_name:
        p = sb.exec("sh", "-c", _STORE_OP_TOKEN)
        if p.wait() != 0:
            error = p.stderr.read()
            sb.terminate()
            raise RuntimeError(f"Failed to store the 1Password token: {error}")
    if workdir:
        # SSH logins read this to start in the override instead of /workdir.
        # Keep the path in step with scripts/workdir-profile.sh.
        p = sb.exec("sh", "-c", _STORE_WORKDIR, "sh", workdir)
        if p.wait() != 0:
            error = p.stderr.read()
            sb.terminate()
            raise RuntimeError(f"Failed to record the working directory: {error}")
    return sb


def list_sandboxes() -> list[str]:
    sandboxes = modal.Sandbox.list(app_id=get_app().app_id)
    return [sb.object_id for sb in sandboxes]


def execute(command: str, sandbox_id: str):
    sb = modal.Sandbox.from_id(sandbox_id=sandbox_id)
    p = sb.exec(*shlex.split(command))
    if p.wait() != 0:
        raise RuntimeError(f"Failed to execute command: {p.stderr.read()}")
    return p.stdout.read()


def terminate(sandbox_id: str):
    sb = modal.Sandbox.from_id(sandbox_id=sandbox_id)
    sb.terminate(wait=True)
    return {"id": sb.object_id, "status": "terminated"}
