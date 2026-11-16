"""`agentix auth`: agent-auth-sync on this computer.

agent-auth-sync keeps Codex's and pi's sign-in files in step with 1Password
on every machine that runs it; the image runs it in each sandbox. The script
stands alone in nix/packages/agent-auth-sync and needs only a POSIX shell,
`op`, and `jq`; these commands run this checkout's copy on the host, where
`install` copies it to ~/.local/bin and starts it as a launchd agent or a
systemd user service.

`agentix auth refresher` manages the login refresher (refresher.py), the
scheduled Modal function that alone refreshes those logins.
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


# --- the login refresher (refresher.py), on Modal ----------------------------


def _function():
    import modal

    from . import refresher
    from .errors import NotFound

    function = modal.Function.from_name(refresher.APP_NAME, refresher.FUNCTION_NAME)
    try:
        function.hydrate()
    except modal.exception.NotFoundError:
        raise NotFound(
            f"The login refresher {refresher.APP_NAME} is not deployed. Deploy it "
            "with: agentix auth refresher deploy"
        ) from None
    return function


def _fields() -> dict[str, str]:
    from . import refresher

    return {
        "store": f"{refresher.setting('AGENTIX_REFRESHER_CODEX_STORE')} "
        f"{refresher.setting('AGENTIX_REFRESHER_PI_STORE')}",
        "public": f"{refresher.setting('AGENTIX_REFRESHER_CODEX_PUBLIC')} "
        f"{refresher.setting('AGENTIX_REFRESHER_PI_PUBLIC')}",
    }


def refresher_deploy() -> dict[str, str]:
    from . import refresher

    refresher.app.deploy()
    return {
        "app": refresher.APP_NAME,
        "function": refresher.FUNCTION_NAME,
        "schedule": f"{refresher.SCHEDULE} (UTC)",
        "secret": refresher.SECRET_NAME,
        **_fields(),
    }


def refresher_status() -> dict:
    import modal

    from . import refresher
    from .errors import NotFound

    try:
        _function()
        deployed = True
    except NotFound:
        deployed = False
    last = None
    try:
        last = modal.Dict.from_name(refresher.STATUS_DICT).get("last")
    except modal.exception.NotFoundError:
        pass
    return {
        "app": refresher.APP_NAME,
        "deployed": deployed,
        "schedule": f"{refresher.SCHEDULE} (UTC)",
        **_fields(),
        "last_run": last if last is not None else "never",
    }


def refresher_run(force: bool = False) -> dict:
    summary = _function().remote(force=force)
    if not summary.get("ok"):
        from .cli import print_result

        print_result(summary)
        raise OperationFailed("The login refresher's run did not finish; see above.")
    return summary


def refresher_stop() -> dict[str, str]:
    import modal

    from . import refresher

    try:
        modal.App.lookup(refresher.APP_NAME).stop()
    except modal.exception.NotFoundError:
        return {"app": refresher.APP_NAME, "state": "not deployed"}
    return {"app": refresher.APP_NAME, "state": "stopped"}


def default_files() -> dict[str, str]:
    """Codex's and pi's sign-in files on this computer."""
    import os

    codex_home = os.environ.get("CODEX_HOME") or "~/.codex"
    pi_dir = os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent"
    return {"codex": f"{codex_home}/auth.json", "pi": f"{pi_dir}/auth.json"}


def refresher_seed(codex: str | None = None, pi: str | None = None) -> dict:
    import os

    from . import refresher

    files = default_files()
    codex_path = os.path.expanduser(files["codex"] if codex is None else codex)
    pi_path = os.path.expanduser(files["pi"] if pi is None else pi)
    if not codex_path and not pi_path:
        raise OperationFailed("Nothing to seed: --codex and --pi are both empty.")
    try:
        return refresher.seed(refresher.OpStore(), codex_path, pi_path)
    except (refresher.StoreError, OSError, ValueError) as error:
        raise OperationFailed(str(error)) from None
