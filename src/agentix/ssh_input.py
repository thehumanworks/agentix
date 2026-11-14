"""Checks on SSH input. They need no sandbox, so they run before any is contacted."""

import subprocess
from pathlib import Path

from .errors import InvalidRequest

ENGINES = ("tailscale", "openssh")
NEED_PUBLIC_KEY = "Provide --public-key=PATH to an SSH public key (.pub file)."


def check_arguments(engine: str, hostname: str = "", public_key: str = "") -> None:
    if engine not in ENGINES:
        raise InvalidRequest(
            f"Unsupported SSH engine: {engine}. Choose tailscale or openssh."
        )
    if public_key and engine != "openssh":
        raise InvalidRequest("--public-key is only used by the openssh engine.")
    if hostname and engine != "tailscale":
        raise InvalidRequest("--hostname is only used by the tailscale engine.")


def check_tailscale_start(tailscale: bool, hostname: str, secret_name: str) -> None:
    """Checks on `--tailscale` for a new sandbox, made before it is created."""
    if hostname and not tailscale:
        raise InvalidRequest("--hostname is only used with --tailscale.")
    if tailscale and not secret_name:
        raise InvalidRequest(
            "--tailscale needs the 1Password secret: a new sandbox enrolls with "
            "a key from 1Password. Drop --secret-name='' to use the default."
        )


def check_public_key(key: str, source: str = "the given key") -> str:
    """Return `key` if it is one SSH public key. `source` names it in errors."""
    key = key.strip()
    if "\n" in key or not key.startswith(
        ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-")
    ):
        raise InvalidRequest(
            "Expected one SSH public key, without authorized_keys options."
        )
    check = subprocess.run(
        ["ssh-keygen", "-l", "-f", "-"],
        input=key + "\n",
        text=True,
        capture_output=True,
    )
    if check.returncode:
        raise InvalidRequest(f"Invalid SSH public key: {source}")
    return key


def read_public_key(path: str = "") -> str:
    """Read the public key at `path`, or the first default key of this user."""
    candidates = (
        [Path(path).expanduser()]
        if path
        else [Path.home() / ".ssh/id_ed25519.pub", Path.home() / ".ssh/id_rsa.pub"]
    )
    key_file = next(
        (candidate for candidate in candidates if candidate.is_file()), None
    )
    if key_file is None:
        raise InvalidRequest(NEED_PUBLIC_KEY)
    return check_public_key(key_file.read_text(), str(key_file))
