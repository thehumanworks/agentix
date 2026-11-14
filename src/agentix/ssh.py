"""Control SSH inside an existing sandbox."""

import asyncio
import functools
import json
import shlex
from pathlib import Path
from typing import Any

import modal

from .errors import InvalidRequest, OperationFailed, PublicKeyRequired
from .session import Session
from .ssh_input import NEED_PUBLIC_KEY, check_arguments, check_public_key

_SCRIPTS = Path(__file__).with_name("scripts")
# Keep the text in step with scripts/openssh-ssh.sh.
_KEY_REQUIRED = "An SSH public key is required."


@functools.cache
def _script(engine: str) -> str:
    return (_SCRIPTS / f"{engine}-ssh.sh").read_text()


async def _run(
    handle: modal.Sandbox, engine: str, action: str, argument: str = ""
) -> dict[str, Any]:
    process = await handle.exec.aio(
        "bash", "-c", _script(engine), "agentix-ssh", action, argument, timeout=90
    )
    output, errors, exit_code = await asyncio.gather(
        process.stdout.read.aio(), process.stderr.read.aio(), process.wait.aio()
    )
    if exit_code != 0:
        if errors.strip() == _KEY_REQUIRED:
            raise PublicKeyRequired(NEED_PUBLIC_KEY)
        raise OperationFailed(
            f"SSH {action} failed for {handle.object_id}: {errors.strip()}"
        )
    result = json.loads(output)
    if action == "start" and result["status"] != "running":
        raise OperationFailed(f"SSH did not start for {handle.object_id}.")
    return result


async def manage(
    session: Session,
    action: str,
    sandbox_id: str,
    engine: str = "tailscale",
    hostname: str = "",
    public_key: str = "",
) -> dict[str, Any]:
    """Run `action` (start, stop or status) for one SSH engine of a sandbox.

    `public_key` is the text of the key, not a path. The openssh engine uses it
    only when it starts a listener; a listener that already runs keeps its key.
    """
    check_arguments(engine, hostname, public_key)
    if public_key:
        # ssh-keygen runs as a child process. Do not hold up other requests.
        public_key = await asyncio.to_thread(check_public_key, public_key)
    async with session.sandbox(sandbox_id) as handle:
        if engine == "tailscale":
            result = await _run(handle, engine, action, hostname)
            host = result.get("magicdns") or result.get("ipv4")
            if host:
                result["ssh_command"] = shlex.join(["ssh", f"root@{host}"])
        elif action == "start":
            # Without a tunnel, a listener is of no use. Do not start one.
            tunnel = (await handle.tunnels.aio()).get(22)
            if tunnel is None:
                raise InvalidRequest(
                    "This sandbox has no SSH tunnel. "
                    "Create a new one with agentix sandbox up."
                )
            result = await _run(handle, engine, action, public_key)
        else:
            tunnels, result = await asyncio.gather(
                handle.tunnels.aio(), _run(handle, engine, action)
            )
            tunnel = tunnels.get(22)
        if engine == "openssh" and tunnel is not None:
            host, port = tunnel.tcp_socket
            result.update(
                host=host,
                port=port,
                user="root",
                ssh_command=shlex.join(["ssh", "-p", str(port), f"root@{host}"]),
            )
    return {"sandbox_id": sandbox_id, "engine": engine, **result}
