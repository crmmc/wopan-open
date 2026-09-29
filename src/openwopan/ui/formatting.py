"""Shared pure formatting helpers for UI modules."""

from __future__ import annotations

from openwopan.wopan.models import WopanItemKind


def format_size(size: int | None, kind: WopanItemKind) -> str:
    """Format an item size for display; folders and unknown sizes show '-'."""
    if kind is WopanItemKind.FOLDER:
        return "-"
    if size is None:
        return "-"
    return format_bytes(size)


def format_optional_bytes(size: int | None) -> str:
    """Format an optional byte count; unknown sizes show '-'."""
    if size is None:
        return "-"
    return format_bytes(size)


def format_bytes(size: int) -> str:
    """Format a byte count with human-friendly binary units."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size)
    unit = units[0]
    for unit in units:  # pragma: no branch - docs/testing-exemptions.md
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"
