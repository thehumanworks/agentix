"""`agentix auth`: agent-auth-sync on this computer.

agent-auth-sync keeps Codex's and pi's sign-in files in step with 1Password
on every machine that runs it; the image runs it in each sandbox. The script
stands alone in nix/packages/agent-auth-sync and needs only a POSIX shell,
`op`, and `jq`; these commands run this checkout's copy on the host, where
`install` copies it to ~/.local/bin and starts it as a launchd agent or a
systemd user service.
"""

import subprocess

from .errors import OperationFailed
from .layout import NIX_DIR

SCRIPT = NIX_DIR / "packages" / "agent-auth-sync" / "agent-auth-sync"


def run(*args: str) -> None:
    """Run the script with this process's environment and terminal."""
    result = subprocess.run(["/bin/sh", str(SCRIPT), *args], check=False)
    if result.returncode != 0:
        raise OperationFailed(
            f"agent-auth-sync {' '.join(args)} failed (exit {result.returncode})"
        )
