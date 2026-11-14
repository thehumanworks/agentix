"""The operations that every way of reaching Agentix offers, and how to pick one."""

import os
from collections.abc import Sequence
from typing import Any, Protocol

from .defaults import SERVER_SOCKET
from .errors import ServerUnavailable


class Backend(Protocol):
    def create_sandbox(
        self,
        image_name: str,
        secret_name: str,
        timeout: int,
        workdir: str,
        memory: int,
        cpu: float,
    ) -> dict[str, str]: ...

    def list_sandboxes(self) -> list[str]: ...

    def execute(self, sandbox_id: str, args: Sequence[str]) -> dict[str, Any]: ...

    def terminate(self, sandbox_id: str) -> dict[str, str]: ...

    def ssh(
        self,
        action: str,
        sandbox_id: str,
        engine: str,
        hostname: str = "",
        public_key: str = "",
    ) -> dict[str, Any]: ...


def connect() -> Backend:
    """Use a running Agentix server if there is one, else work in this process.

    `AGENTIX_SERVER` selects the server: a socket path that must answer, or
    `off` to always work in this process. If it is not set, the default socket
    is used when a server listens there.
    """
    target = os.environ.get("AGENTIX_SERVER", "")
    if target == "off":
        return _local()
    # The client needs only the standard library. Modal is slow to import, so
    # load it only when this process does the work itself.
    from .client import Remote

    remote = Remote(target or SERVER_SOCKET)
    try:
        remote.open()
    except ServerUnavailable:
        if target:
            raise
        return _local()
    return remote


def _local() -> Backend:
    from .local import Local

    return Local()
