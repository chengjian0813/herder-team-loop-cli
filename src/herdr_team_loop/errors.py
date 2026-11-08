"""Structured failures shared by application services and execution adapters."""


class LoopError(Exception):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def require(condition: object, code: str, message: str, **details: object) -> None:
    if not condition:
        raise LoopError(code, message, details=details)
