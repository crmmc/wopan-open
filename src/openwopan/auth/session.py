from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthSession:
    """OpenWoPan-owned authenticated session summary."""

    account_id: str
    display_name: str | None = None
    token_preview: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")
