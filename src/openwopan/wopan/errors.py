from __future__ import annotations


class WopanError(Exception):
    """Base error for WoPan protocol-layer failures."""


class WopanResponseError(WopanError):
    """Raised when a WoPan response cannot be parsed as expected."""


class WopanAuthenticationError(WopanError):
    """Raised when WoPan reports that the login session is no longer valid."""


class WopanBusinessError(WopanError):
    """Raised when WoPan returns a non-success business code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"
