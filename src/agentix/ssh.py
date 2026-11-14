"""Control SSH inside an existing sandbox from the local CLI."""

import json
import shlex
import subprocess
from pathlib import Path

import modal

_SCRIPTS = Path(__file__).with_name("scripts")


def _run(sb, engine: str, action: str, argument: str = ""):
    process = sb.exec(
        "bash",
        "-c",
        (_SCRIPTS / f"{engine}-ssh.sh").read_text(),
        "agentix-ssh",
        action,
        argument,
        timeout=90,
    )
    output = process.stdout.read()
    errors = process.stderr.read()
    if process.wait() != 0:
        raise RuntimeError(f"SSH {action} failed for {sb.object_id}: {errors.strip()}")
    result = json.loads(output)
    if action == "start" and result["status"] != "running":
        raise RuntimeError(f"SSH did not start for {sb.object_id}.")
    return result


def _public_key(path: str) -> str:
    candidates = (
        [Path(path).expanduser()]
        if path
        else [Path.home() / ".ssh/id_ed25519.pub", Path.home() / ".ssh/id_rsa.pub"]
    )
    key_file = next(
        (candidate for candidate in candidates if candidate.is_file()), None
    )
    if key_file is None:
        raise ValueError("Provide --public-key=PATH to an SSH public key (.pub file).")
    key = key_file.read_text().strip()
    if "\n" in key or not key.startswith(
        ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-")
    ):
        raise ValueError(
            "Expected one SSH public key, without authorized_keys options."
        )
    check = subprocess.run(
        ["ssh-keygen", "-l", "-f", "-"],
        input=key + "\n",
        text=True,
        capture_output=True,
    )
    if check.returncode:
        raise ValueError(f"Invalid SSH public key: {key_file}")
    return key


def _manage(
    action: str, sandbox_id: str, engine: str, hostname: str = "", public_key: str = ""
):
    if engine not in ("tailscale", "openssh"):
        raise ValueError(
            f"Unsupported SSH engine: {engine}. Choose tailscale or openssh."
        )
    if public_key and engine != "openssh":
        raise ValueError("--public-key is only used by the openssh engine.")
    if hostname and engine != "tailscale":
        raise ValueError("--hostname is only used by the tailscale engine.")
    sb = modal.Sandbox.from_id(sandbox_id=sandbox_id)
    if engine == "tailscale":
        result = _run(sb, engine, action, hostname)
        host = result.get("magicdns") or result.get("ipv4")
        if host:
            result["ssh_command"] = shlex.join(["ssh", f"root@{host}"])
    else:
        tunnel = sb.tunnels().get(22)
        if tunnel is None and action == "start":
            raise ValueError(
                "This sandbox has no SSH tunnel. Create a new one with agentix sandbox up."
            )
        result = _run(sb, engine, "status" if action == "start" else action)
        if action == "start" and result["status"] != "running":
            result = _run(sb, engine, action, _public_key(public_key))
        if tunnel is not None:
            host, port = tunnel.tcp_socket
            result.update(
                host=host,
                port=port,
                user="root",
                ssh_command=shlex.join(["ssh", "-p", str(port), f"root@{host}"]),
            )
    return {"sandbox_id": sandbox_id, "engine": engine, **result}


def start(
    sandbox_id: str, engine: str = "tailscale", hostname: str = "", public_key: str = ""
):
    """Start SSH, or return the existing connection details if already running."""
    return _manage("start", sandbox_id, engine, hostname, public_key)


def stop(sandbox_id: str, engine: str = "tailscale"):
    """Stop the selected SSH engine without terminating the sandbox."""
    return _manage("stop", sandbox_id, engine)


def status(sandbox_id: str, engine: str = "tailscale"):
    """Show SSH state and connection details for the given sandbox."""
    return _manage("status", sandbox_id, engine)
