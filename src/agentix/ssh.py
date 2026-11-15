"""Control SSH inside an existing sandbox."""

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
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
    host_keys: bool = False,
) -> dict[str, Any]:
    """Run `action` (start, stop or status) for one SSH engine of a sandbox.

    On start, the Tailscale hostname defaults to `default_hostname`. `name` is
    the name of the sandbox, "" for none, or None to look it up. Explicit
    hostnames also apply when Tailscale is already running.

    `public_key` is the text of the key, not a path. The openssh engine uses it
    only when it starts a listener; a listener that already runs keeps its key.

    With `host_keys`, the result has the public host keys of the SSH server,
    as `TYPE BASE64` lines, read in the sandbox.
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
    keys = result.pop("host_keys", [])
    if host_keys:
        result["host_keys"] = keys
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


async def connect(
    session: Session,
    sandbox_id: str,
    name: str,
    engine: str = "tailscale",
    hostname: str = "",
    public_key: str = "",
) -> dict[str, Any]:
    """Start SSH unless it runs, wait until it answers, and say how to log in.

    Returns `destination` and `ssh_options` for `ssh`. The options make ssh
    accept only the host keys that the sandbox reported through Modal, from a
    known_hosts file of agentix: it never asks, and the user's own
    known_hosts file is neither read nor changed.

    SSH that runs already is used as it is, with its hostname, unless
    `hostname` asks for another one.
    """
    details = None
    if not hostname:
        details = await manage(session, "status", sandbox_id, engine, host_keys=True)
    # A Tailscale node can run and still be offline: start reconnects it.
    if (
        details is None
        or details["status"] != "running"
        or details.get("online") is False
    ):
        details = await manage(
            session,
            "start",
            sandbox_id,
            engine,
            hostname,
            public_key,
            name=name,
            host_keys=True,
        )
    options = []
    if engine == "tailscale":
        host = details["magicdns"] or details["ipv4"]
        # Connecting to the address needs no DNS, which can lag behind a node
        # that just enrolled. ssh still matches its configuration on the name.
        address, port = details["ipv4"] or host, 22
        if address != host:
            options += ["-o", f"HostName={address}"]
        await wait_until_reachable(address, port, engine)
        keys = details["host_keys"]
    else:
        if "host" not in details:
            raise InvalidRequest(
                "This sandbox has no SSH tunnel. "
                "Create a new one with agentix sandbox up."
            )
        host = address = details["host"]
        port = details["port"]
        options += ["-p", str(port)]
        await wait_until_reachable(address, port, engine)
        # The sandbox reports the fingerprint of its key. The key itself comes
        # from the server, and only one with that fingerprint is kept.
        expected = details.get("host_key_fingerprint", "")
        keys = await asyncio.to_thread(scan_host_keys, address, port, expected)
        if expected and not keys:
            raise OperationFailed(
                f"The SSH server at {address}:{port} did not present the host key "
                f"that {sandbox_id} reported ({expected}), so ssh was not started."
            )
    if not keys:
        raise OperationFailed(
            f"SSH runs in {sandbox_id}, but its host key was not found, so its "
            "identity cannot be checked."
        )
    # One file per engine: each has host keys of its own.
    alias = f"agentix-{name or sandbox_id.lower()}-{engine}"
    known_hosts = await asyncio.to_thread(write_known_hosts, alias, keys)
    options += [
        "-o",
        f"HostKeyAlias={alias}",
        "-o",
        f'UserKnownHostsFile="{known_hosts}"',
        "-o",
        "StrictHostKeyChecking=yes",
    ]
    result = {
        "sandbox_id": sandbox_id,
        "name": name,
        "engine": engine,
        "destination": f"root@{host}",
        "ssh_options": options,
        "host_key_fingerprints": [fingerprint(key) for key in keys],
    }
    if "warning" in details:
        result["warning"] = details["warning"]
    return result


# How long to wait for a started SSH server to answer. A new Tailscale node
# reaches the other nodes of the tailnet within seconds.
REACHABLE_TIMEOUT = 60


async def wait_until_reachable(
    host: str, port: int, engine: str, timeout: float = REACHABLE_TIMEOUT
) -> None:
    """Wait until an SSH server at host:port sends its greeting."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), 5
            )
            try:
                # A server may send other lines before its version.
                for _ in range(10):
                    greeting = await asyncio.wait_for(reader.readline(), 5)
                    if not greeting or greeting.startswith(b"SSH-"):
                        break
            finally:
                writer.close()
            if greeting.startswith(b"SSH-"):
                return
            problem = f"it answered {greeting[:40]!r}"
        except (OSError, TimeoutError) as error:
            problem = str(error) or type(error).__name__
        if loop.time() >= deadline:
            hint = (
                " Is this computer on the tailnet, and does its policy allow SSH?"
                if engine == "tailscale"
                else ""
            )
            raise OperationFailed(
                f"SSH at {host}:{port} did not answer within {timeout:g} s "
                f"({problem}).{hint}"
            )
        await asyncio.sleep(0.5)


def fingerprint(key: str) -> str:
    """The SHA256 fingerprint of a `TYPE BASE64` public key, as ssh shows it."""
    blob = base64.b64decode(key.split()[1], validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode()
    return "SHA256:" + digest.rstrip("=")


def scan_host_keys(host: str, port: int, expected: str) -> list[str]:
    """The host keys that the server at host:port presents with `expected`."""
    scan = subprocess.run(
        ["ssh-keyscan", "-T", "10", "-p", str(port), host],
        capture_output=True,
        text=True,
    )
    keys = []
    for line in scan.stdout.splitlines():
        fields = line.split()
        if line.startswith("#") or len(fields) < 3:
            continue
        key = f"{fields[1]} {fields[2]}"
        with contextlib.suppress(ValueError, IndexError):
            if fingerprint(key) == expected:
                keys.append(key)
    return keys


def known_hosts_directory() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "agentix" / "known_hosts"


# Every login rewrites the known_hosts file of its sandbox. One that is older
# than this is of a sandbox that nobody logged in to for a month.
_KNOWN_HOSTS_AGE = 30 * 24 * 3600


def write_known_hosts(alias: str, keys: list[str]) -> Path:
    """Write the host keys of `alias` to a file of its own, and return its path.

    Each sandbox has its own file, so concurrent logins to different
    sandboxes do not interfere. A file is replaced, never edited.
    """
    directory = known_hosts_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time()
    for old in directory.iterdir():
        with contextlib.suppress(OSError):
            if now - old.stat().st_mtime > _KNOWN_HOSTS_AGE:
                old.unlink()
    path = directory / alias
    staged = path.with_name(f".{alias}.{os.getpid()}")
    staged.write_text("".join(f"{alias} {key}\n" for key in keys))
    staged.replace(path)
    return path
