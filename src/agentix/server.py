"""Keep Agentix warm for the command line.

The server keeps one `Session`, so every command reuses the connections that
earlier commands made. It listens on a Unix socket for this user only. Run it
with `agentix serve`.
"""

import asyncio
import contextlib
import os
import socket
import stat
from collections.abc import AsyncIterator
from typing import Annotated

import modal
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import sandbox, ssh
from .defaults import (
    IMAGE_NAME,
    SANDBOX_MEMORY_MIB,
    SANDBOX_TIMEOUT,
    SECRET_NAME,
    SERVER_SOCKET,
)
from .errors import AgentixError, InvalidRequest
from .session import Session, describe


class SandboxCreate(BaseModel):
    image_name: str = IMAGE_NAME
    secret_name: str = SECRET_NAME
    timeout: int = Field(SANDBOX_TIMEOUT, gt=0)
    workdir: str = ""
    memory: int = Field(SANDBOX_MEMORY_MIB, gt=0)
    # Zero keeps Modal's default.
    cpu: float = Field(0, ge=0)


class SandboxCreated(BaseModel):
    sandbox_id: str


class SandboxList(BaseModel):
    sandboxes: list[str]


class SandboxTerminated(BaseModel):
    id: str
    status: str


class ExecRequest(BaseModel):
    args: list[str] = Field(min_length=1)


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class SSHStart(BaseModel):
    engine: str = "tailscale"
    hostname: str = ""
    # The text of the key, not a path. The server may not see the caller's files.
    public_key: str = ""


class SSHState(BaseModel):
    model_config = ConfigDict(extra="allow")

    sandbox_id: str
    engine: str
    status: str
    magicdns: str | None = None
    ipv4: str | None = None
    host_key_fingerprint: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    ssh_command: str | None = None


def create_app(session: Session | None = None) -> FastAPI:

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.session = session or Session()
        # Connect to Modal before the first request needs it.
        warm = asyncio.ensure_future(_quietly(app.state.session.app()))
        yield
        warm.cancel()
        await app.state.session.close()

    def current(request: Request) -> Session:
        return request.app.state.session

    app = FastAPI(title="Agentix", lifespan=lifespan)
    Shared = Annotated[Session, Depends(current)]

    @app.exception_handler(AgentixError)
    @app.exception_handler(modal.exception.Error)
    async def failed(request: Request, error: Exception) -> JSONResponse:
        return _problem(describe(error))

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError) -> JSONResponse:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        return _problem(InvalidRequest(details))

    @app.post("/v1/sandboxes", status_code=201)
    async def create_sandbox(body: SandboxCreate, session: Shared) -> SandboxCreated:
        sandbox_id = await sandbox.create(
            session,
            body.image_name,
            body.secret_name,
            body.timeout,
            body.workdir,
            body.memory,
            body.cpu,
        )
        return SandboxCreated(sandbox_id=sandbox_id)

    @app.get("/v1/sandboxes")
    async def list_sandboxes(session: Shared) -> SandboxList:
        return SandboxList(sandboxes=await sandbox.list_ids(session))

    @app.delete("/v1/sandboxes/{sandbox_id}")
    async def terminate_sandbox(sandbox_id: str, session: Shared) -> SandboxTerminated:
        return SandboxTerminated(**await sandbox.terminate(session, sandbox_id))

    @app.post("/v1/sandboxes/{sandbox_id}/exec")
    async def execute(
        sandbox_id: str, body: ExecRequest, session: Shared
    ) -> ExecResponse:
        """Run a command. A non-zero exit code is a result, not an error."""
        return ExecResponse(**await sandbox.execute(session, sandbox_id, body.args))

    ssh_options = {"response_model_exclude_none": True}

    @app.post("/v1/sandboxes/{sandbox_id}/ssh", **ssh_options)
    async def start_ssh(sandbox_id: str, body: SSHStart, session: Shared) -> SSHState:
        """Start SSH, or return the existing connection details if already running."""
        return SSHState(
            **await ssh.manage(
                session,
                "start",
                sandbox_id,
                body.engine,
                body.hostname,
                body.public_key,
            )
        )

    @app.get("/v1/sandboxes/{sandbox_id}/ssh", **ssh_options)
    async def ssh_status(
        sandbox_id: str, session: Shared, engine: str = "tailscale"
    ) -> SSHState:
        return SSHState(**await ssh.manage(session, "status", sandbox_id, engine))

    @app.delete("/v1/sandboxes/{sandbox_id}/ssh", **ssh_options)
    async def stop_ssh(
        sandbox_id: str, session: Shared, engine: str = "tailscale"
    ) -> SSHState:
        """Stop the selected SSH engine without terminating the sandbox."""
        return SSHState(**await ssh.manage(session, "stop", sandbox_id, engine))

    return app


def serve(socket_path: str = "") -> None:
    """Run the server until it is interrupted."""
    import uvicorn

    path = os.path.expanduser(socket_path or SERVER_SOCKET)
    listener = _listen(path)
    try:
        uvicorn.Server(uvicorn.Config(create_app())).run(sockets=[listener])
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _listen(path: str) -> socket.socket:
    """Bind a Unix socket that only this user can connect to."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(path)
        except OSError:
            pass
        else:
            raise InvalidRequest(f"An Agentix server already listens at {path}.")
    # A server that stopped without cleanup leaves its socket behind.
    if stat.S_ISSOCK(_mode(path)):
        os.unlink(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous_mask = os.umask(0o177)
    try:
        listener.bind(path)
    except OSError as error:
        listener.close()
        raise InvalidRequest(f"Cannot listen at {path}: {error}") from error
    finally:
        os.umask(previous_mask)
    return listener


def _mode(path: str) -> int:
    try:
        return os.stat(path).st_mode
    except FileNotFoundError:
        return 0


def _problem(error: AgentixError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": error.code, "message": str(error)}},
        status_code=error.status,
    )


async def _quietly(work) -> None:
    with contextlib.suppress(Exception):
        await work
