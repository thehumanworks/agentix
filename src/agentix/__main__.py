"""The command line. It only parses input and prints results.

Modal is slow to import, so nothing here imports it. A command that a running
server can do never loads it.
"""

import shlex

from .defaults import IMAGE_NAME, SANDBOX_MEMORY_MIB, SANDBOX_TIMEOUT, SECRET_NAME
from .errors import PublicKeyRequired


def _backend():
    from .backend import connect

    return connect()


def _build_image(force_build: bool = False, image_name: str = IMAGE_NAME):
    from .image import build_image

    image = build_image(force_build=force_build, image_name=image_name)
    return {"image_id": image.object_id}


def _create_sandbox(
    image_name: str = IMAGE_NAME,
    secret_name: str = SECRET_NAME,
    timeout: int = SANDBOX_TIMEOUT,
    workdir: str = "",
    memory: int = SANDBOX_MEMORY_MIB,
    cpu: float = 0,
):
    return _backend().create_sandbox(
        image_name=image_name,
        secret_name=secret_name,
        timeout=timeout,
        workdir=workdir,
        memory=memory,
        cpu=cpu,
    )


def _terminate_sandbox(sandbox_id: str):
    return _backend().terminate(sandbox_id)


def _execute(sandbox_id: str, command: str):
    result = _backend().execute(sandbox_id, shlex.split(command))
    if result["exit_code"] != 0:
        raise RuntimeError(f"Failed to execute command: {result['stderr']}")
    return result["stdout"]


def _list_sandboxes():
    return _backend().list_sandboxes()


def _ssh_start(
    sandbox_id: str, engine: str = "tailscale", hostname: str = "", public_key: str = ""
):
    """Start SSH. Tailscale names default to the lowercase sandbox ID.

    An explicit hostname also applies when Tailscale is already running.
    """
    from . import ssh_input

    ssh_input.check_arguments(engine, hostname, public_key)
    key, key_problem = "", None
    if engine == "openssh":
        # A listener that already runs needs no key. Report a key problem only
        # if the sandbox asks for the key.
        try:
            key = ssh_input.read_public_key(public_key)
        except ValueError as error:
            key_problem = error
    try:
        return _backend().ssh("start", sandbox_id, engine, hostname, key)
    except PublicKeyRequired:
        if key_problem is None:
            raise
        raise key_problem from None


def _ssh_stop(sandbox_id: str, engine: str = "tailscale"):
    """Stop the selected SSH engine without terminating the sandbox."""
    return _backend().ssh("stop", sandbox_id, engine)


def _ssh_status(sandbox_id: str, engine: str = "tailscale"):
    """Show SSH state and connection details for the given sandbox."""
    return _backend().ssh("status", sandbox_id, engine)


def _serve(socket: str = ""):
    """Keep Agentix warm. While this runs, the sandbox commands are faster."""
    from .server import serve

    serve(socket_path=socket)


def main():
    from fire import Fire

    Fire(
        {
            "image": {
                "build": _build_image,
            },
            "sandbox": {
                "up": _create_sandbox,
                "down": _terminate_sandbox,
                "exec": _execute,
                "ls": _list_sandboxes,
                "ssh": {
                    "start": _ssh_start,
                    "stop": _ssh_stop,
                    "status": _ssh_status,
                },
            },
            "serve": _serve,
        }
    )


if __name__ == "__main__":
    import os

    os.environ["PAGER"] = "cat"
    main()
