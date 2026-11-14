"""Send operations to the Agentix server. Uses only the standard library."""

import http.client
import json
import os
import socket
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlencode

from .errors import OperationFailed, ServerUnavailable, from_code


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


class Remote:
    def __init__(self, socket_path: str):
        self._target = socket_path
        self._connection = _UnixConnection(os.path.expanduser(socket_path))

    def open(self) -> None:
        """Connect now, so that a missing server shows before a request is sent."""
        try:
            self._connection.connect()
        except OSError as error:
            raise ServerUnavailable(
                f"No Agentix server answers at {self._target}: {error}"
            ) from error

    def close(self) -> None:
        self._connection.close()

    def create_sandbox(
        self,
        image_name: str,
        secret_name: str,
        timeout: int,
        workdir: str,
        memory: int,
        cpu: float,
    ) -> dict[str, str]:
        return self._call(
            "POST",
            "/v1/sandboxes",
            {
                "image_name": image_name,
                "secret_name": secret_name,
                "timeout": timeout,
                "workdir": workdir,
                "memory": memory,
                "cpu": cpu,
            },
        )

    def list_sandboxes(self) -> list[str]:
        return self._call("GET", "/v1/sandboxes")["sandboxes"]

    def execute(self, sandbox_id: str, args: Sequence[str]) -> dict[str, Any]:
        return self._call("POST", f"{_sandbox(sandbox_id)}/exec", {"args": list(args)})

    def terminate(self, sandbox_id: str) -> dict[str, str]:
        return self._call("DELETE", _sandbox(sandbox_id))

    def ssh(
        self,
        action: str,
        sandbox_id: str,
        engine: str,
        hostname: str = "",
        public_key: str = "",
    ) -> dict[str, Any]:
        path = f"{_sandbox(sandbox_id)}/ssh"
        if action == "start":
            body = {"engine": engine, "hostname": hostname, "public_key": public_key}
            return self._call("POST", path, body)
        method = {"status": "GET", "stop": "DELETE"}[action]
        return self._call(method, f"{path}?{urlencode({'engine': engine})}")

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            self._connection.request(
                method, path, None if body is None else json.dumps(body), headers
            )
            response = self._connection.getresponse()
            content = response.read()
        except (OSError, http.client.HTTPException) as error:
            self._connection.close()
            raise ServerUnavailable(
                f"The Agentix server at {self._target} did not answer: {error!r}"
            ) from error
        try:
            data = json.loads(content)
        except ValueError:
            raise OperationFailed(
                f"The Agentix server sent an unreadable reply (HTTP {response.status})."
            ) from None
        if response.status >= 400:
            problem = data.get("error", {}) if isinstance(data, dict) else {}
            raise from_code(
                problem.get("code", ""),
                problem.get("message", f"HTTP {response.status}"),
            )
        return data


def _sandbox(sandbox_id: str) -> str:
    return f"/v1/sandboxes/{quote(sandbox_id, safe='')}"
