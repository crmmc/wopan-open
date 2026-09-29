"""Shared pure formatting helpers for UI modules."""

from __future__ import annotations

from collections.abc import Sequence

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


def format_kind(kind: WopanItemKind) -> str:
    """Format an item kind for display."""
    if kind is WopanItemKind.FOLDER:
        return "文件夹"
    return "文件"


def format_items_summary(names: Sequence[str]) -> str:
    """Summarize item names for a confirmation prompt.

    Single item renders as 「name」; multiple items render as
    `` N 个对象（preview）`` with at most three previewed names.
    """
    if len(names) == 1:
        return f"「{names[0]}」"
    preview = "、".join(names[:3])
    if len(names) > 3:
        preview += " 等"
    return f" {len(names)} 个对象（{preview}）"
