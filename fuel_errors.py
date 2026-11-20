"""Shared fuel-engine domain exceptions."""


class FuelError(Exception):
    """HTTP-safe domain error carrying the status expected by the legacy API."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
