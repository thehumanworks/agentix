"""The command line. It only parses input and prints results.

Modal is slow to import, so only a command that needs it loads it.
"""

import shlex

from .defaults import IMAGE_NAME, SANDBOX_MEMORY_MIB, SANDBOX_TIMEOUT, SECRET_NAME
from .errors import PublicKeyRequired


def _backend():
    from .local import Local

    return Local()


def _build_image(force_build: bool = False, image_name: str = IMAGE_NAME):
    from .image import build_image

    image = build_image(force_build=force_build, image_name=image_name)
    return {"image_id": image.object_id}


def _flake_update(*inputs: str, build: bool = False, image_name: str = IMAGE_NAME):
    """Run `nix flake update` in a sandbox and write nix/flake.lock on this host.

    Name inputs to move only those, for example `flake update claude-mods`;
    with no names every input moves except Rex, which moves only when named,
    as in `flake update rex`: it then points nix/flake.nix at the newest Rex
    snapshot. The lock is written only once the image's home profile
    evaluates with it. With `--build`, the image is built and published if an
    input moved; otherwise run `image build` afterwards.
    """
    from .flake import update
    from .image import get_app

    result = update(get_app(), image_name, inputs)
    if build and result["changed"]:
        result |= _build_image(image_name=image_name)
    return result


def _create_sandbox(
    image_name: str = IMAGE_NAME,
    secret_name: str = SECRET_NAME,
    timeout: int = SANDBOX_TIMEOUT,
    workdir: str = "",
    memory: int = SANDBOX_MEMORY_MIB,
    cpu: float = 0,
    name: str = "",
    tailscale: bool = False,
    hostname: str = "",
    no_snapshot: bool = False,
    ephemeral: bool = False,
):
    """Start a sandbox under NAME, or under a new name such as brisk-otter.

    Its filesystem is saved when it ends, for `sandbox restore`, unless
    --no-snapshot is given. --ephemeral starts a sandbox with no name, no
    record, and no snapshot: `sandbox down ID` leaves nothing behind. With
    --tailscale, Tailscale SSH starts too, as with `sandbox ssh start`.
    """
    from . import names, ssh_input

    # Refuse before anything is created.
    names.check_ephemeral(name, ephemeral)
    if name:
        names.check_new_name(name)
    ssh_input.check_tailscale_start(tailscale, hostname, secret_name)
    return _backend().create_sandbox(
        image_name=image_name,
        secret_name=secret_name,
        timeout=timeout,
        workdir=workdir,
        memory=memory,
        cpu=cpu,
        name=name,
        tailscale=tailscale,
        hostname=hostname,
        exit_snapshot=not (no_snapshot or ephemeral),
        ephemeral=ephemeral,
    )


def _terminate_sandbox(sandbox_id: str):
    """Stop a sandbox, given its ID or name, and record its snapshot."""
    return _backend().terminate(sandbox_id)


def _execute(sandbox_id: str, command: str):
    """Run COMMAND in a sandbox, given its ID or name."""
    result = _backend().execute(sandbox_id, shlex.split(command))
    if result["exit_code"] != 0:
        raise RuntimeError(f"Failed to execute command: {result['stderr']}")
    return result["stdout"]


def _list_sandboxes():
    """Named sandboxes, running or not, and running sandboxes without a name."""
    return _backend().list_sandboxes()


def _snapshot(name: str):
    """Save the filesystem of the running sandbox of NAME now."""
    return _backend().snapshot(name)


def _snapshots(name: str):
    """The snapshots of NAME, oldest first, with the time each was taken."""
    return _backend().snapshots(name)


def _restore(
    name: str, snapshot: str = "", tailscale: bool = False, hostname: str = ""
):
    """Start a new sandbox for NAME from its latest snapshot, or from --snapshot=ID.

    The sandbox NAME has now must have ended; stop it with `sandbox down`.
    """
    return _backend().restore(name, snapshot, tailscale, hostname)


def _purge(name: str, terminate: bool = False):
    """Delete the snapshots and the record of NAME.

    A running sandbox is refused, unless --terminate stops it first.
    """
    return _backend().purge(name, terminate)


def _ssh_start(
    sandbox_id: str, engine: str = "tailscale", hostname: str = "", public_key: str = ""
):
    """Start SSH in a sandbox, given its ID or name.

    The Tailscale hostname defaults to the sandbox name, so that a restored
    sandbox keeps its address, and to the lowercase sandbox ID for a sandbox
    without a name. An explicit hostname also applies when Tailscale is
    already running.
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


def main():
    from fire import Fire

    Fire(
        {
            "image": {
                "build": _build_image,
            },
            "flake": {
                "update": _flake_update,
            },
            "sandbox": {
                "up": _create_sandbox,
                "down": _terminate_sandbox,
                "exec": _execute,
                "ls": _list_sandboxes,
                "snapshot": _snapshot,
                "snapshots": _snapshots,
                "restore": _restore,
                "purge": _purge,
                "ssh": {
                    "start": _ssh_start,
                    "stop": _ssh_stop,
                    "status": _ssh_status,
                },
            },
        }
    )


if __name__ == "__main__":
    import os

    os.environ["PAGER"] = "cat"
    main()
