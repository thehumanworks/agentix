"""Errors that mean the same thing in-process and through the server.

Each error has a stable `code` and an HTTP `status`. The server sends both, and
the client rebuilds the same class from the code, so callers see one set of
errors whichever way the request travelled.
"""


class AgentixError(Exception):
    code = "error"
    status = 500


class InvalidRequest(AgentixError, ValueError):
    """The caller asked for something that cannot be done as stated."""

    code = "invalid_request"
    status = 400


class PublicKeyRequired(InvalidRequest):
    """OpenSSH is not running yet, and no public key was given to start it."""

    code = "public_key_required"


class NotFound(AgentixError, LookupError):
    code = "not_found"
    status = 404


class OperationFailed(AgentixError, RuntimeError):
    """Modal or the sandbox reported a failure."""

    code = "operation_failed"
    status = 502


class ServerUnavailable(AgentixError, ConnectionError):
    """The configured Agentix server did not answer."""

    code = "server_unavailable"
    status = 503


_BY_CODE = {
    error.code: error
    for error in (
        InvalidRequest,
        PublicKeyRequired,
        NotFound,
        OperationFailed,
    )
}


def from_code(code: str, message: str) -> AgentixError:
    return _BY_CODE.get(code, OperationFailed)(message)
