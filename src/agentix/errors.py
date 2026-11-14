"""Errors that every operation reports, whatever failed underneath."""


class AgentixError(Exception):
    pass


class InvalidRequest(AgentixError, ValueError):
    """The caller asked for something that cannot be done as stated."""


class PublicKeyRequired(InvalidRequest):
    """OpenSSH is not running yet, and no public key was given to start it."""


class NotFound(AgentixError, LookupError):
    pass


class OperationFailed(AgentixError, RuntimeError):
    """Modal or the sandbox reported a failure."""
