"""Control SSH inside an existing sandbox."""

import asyncio
import functools
import json
import re
import shlex
from pathlib import Path
from typing import Any

import modal

from . import names, sandbox
from .errors import InvalidRequest, OperationFailed, PublicKeyRequired
from .layout import WORKDIR, WORKDIR_FILE
from .session import Session
from .ssh_input import NEED_PUBLIC_KEY, check_arguments, check_public_key

_SCRIPTS = Path(__file__).with_name("scripts")
# Keep the text in step with scripts/openssh-ssh.sh.
_KEY_REQUIRED = "An SSH public key is required."


@functools.cache
def _script(engine: str) -> str:
    text = (_SCRIPTS / f"{engine}-ssh.sh").read_text()
    return text.replace("@workdirFile@", WORKDIR_FILE).replace("@workdir@", WORKDIR)


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


def default_hostname(name: str, sandbox_id: str) -> str:
    """The Tailscale hostname of a sandbox that is given none.

    The name of a named sandbox, so that its address outlives the sandbox:
    a restored sandbox gets it again. A sandbox without a name, or with a
    name that is no hostname, gets its lowercase ID, which no other sandbox
    has.
    """
    return name if name and names.is_hostname(name) else sandbox_id.lower()


async def manage(
    session: Session,
    action: str,
    sandbox_id: str,
    engine: str = "tailscale",
    hostname: str = "",
    public_key: str = "",
    name: str | None = None,
) -> dict[str, Any]:
    """Run `action` (start, stop or status) for one SSH engine of a sandbox.

    On start, the Tailscale hostname defaults to `default_hostname`. `name` is
    the name of the sandbox, "" for none, or None to look it up. Explicit
    hostnames also apply when Tailscale is already running.

    `public_key` is the text of the key, not a path. The openssh engine uses it
    only when it starts a listener; a listener that already runs keeps its key.
    """
    check_arguments(engine, hostname, public_key)
    if public_key:
        # ssh-keygen runs as a child process. Do not hold up other requests.
        public_key = await asyncio.to_thread(check_public_key, public_key)
    # The command that starts SSH again, for the warning about a taken name.
    again = shlex.join(["agentix", "sandbox", "ssh", "start", name or sandbox_id])
    if hostname:
        again += f" --hostname={shlex.quote(hostname)}"
    elif engine == "tailscale" and action == "start":
        if name is None:
            name = await sandbox.name_of(session, sandbox_id)
        hostname = default_hostname(name, sandbox_id)
    async with session.sandbox(sandbox_id) as handle:
        if engine == "tailscale":
            result = await _run(handle, engine, action, hostname)
            host = result.get("magicdns") or result.get("ipv4")
            if host:
                result["ssh_command"] = shlex.join(["ssh", f"root@{host}"])
            if action == "start" and (warning := _renamed(result, hostname, again)):
                result["warning"] = warning
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


def _renamed(result: dict[str, Any], hostname: str, again: str) -> str:
    """Explain a MagicDNS name of `hostname-N`, which is not `hostname`.

    Tailscale gives a node `NAME-1` when another node of the tailnet is named
    NAME already, for example the node of an earlier sandbox of that name.
    The start script has tried to take NAME back already.
    """
    label = result.get("magicdns", "").split(".")[0]
    if not re.fullmatch(re.escape(hostname) + r"-[0-9]+", label):
        return ""
    return (
        f"Tailscale named this node {label}, because another node of the "
        f"tailnet is named {hostname}. ssh root@{hostname} reaches that node, "
        "not this sandbox: use the ssh_command above. If the other node is an "
        "earlier sandbox's, Tailscale removes it about an hour after it went "
        f"offline; then {again} takes the name."
    )
