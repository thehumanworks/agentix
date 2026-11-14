import shlex

import modal

from .image import IMAGE_NAME
from .shared import get_app


def create_sandbox(
    image_name: str = IMAGE_NAME, secret_name: str = "1password", timeout: int = 3600
):
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
        runtime="vm",
        unencrypted_ports=[22],
    )
    return sb

def list_sandboxes() -> list[str]:
    sandboxes = modal.Sandbox.list(
        app_id=get_app().app_id
    )
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
