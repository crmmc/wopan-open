"""Shared view-layer table transforms: header-click sort state helpers.

Sorting is a data-level transform applied before each table render; the
widgets never use ``QTableWidget.sortItems`` because size/time columns
display formatted text whose lexicographic order would be wrong. Header
clicks only advance the sort state and re-render; the header sort
indicator is a pure visual echo while ``setSortingEnabled`` stays False.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView

# Status text shown when a filter hides every row of a non-empty list;
# distinct from each page's genuine "list is empty" wording.
NO_MATCH_TEXT = "无匹配项"


@dataclass(frozen=True, slots=True)
class TableSortState:
    """Per-table header-click sort state; ``column is None`` keeps source order."""

    column: int | None = None
    order: Qt.SortOrder = Qt.SortOrder.AscendingOrder


def cycle_sort_state(state: TableSortState, column: int) -> TableSortState:
    """Advance one column's click cycle: none -> asc -> desc -> none."""
    if state.column != column:
        return TableSortState(column=column, order=Qt.SortOrder.AscendingOrder)
    if state.order is Qt.SortOrder.AscendingOrder:
        return TableSortState(column=column, order=Qt.SortOrder.DescendingOrder)
    return TableSortState()


def sorted_view[T](
    entries: Sequence[T],
    state: TableSortState,
    key_functions: Mapping[int, Callable[[T], Any]],
) -> list[T]:
    """Return the sort-applied copy of ``entries`` (stable; input untouched).

    An inactive state (``column is None``) or a column without a key
    function keeps the source order, so non-sortable columns can never
    reorder rows even if their header click reaches this helper.
    """
    if state.column is None or state.column not in key_functions:
        return list(entries)
    reverse = state.order is Qt.SortOrder.DescendingOrder
    return sorted(entries, key=key_functions[state.column], reverse=reverse)


def sync_sort_indicator(header: QHeaderView | None, state: TableSortState) -> None:
    """Mirror the sort state onto the header arrow (visual only, no sorting)."""
    if header is None:
        return
    if state.column is None:
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        return
    header.setSortIndicator(state.column, state.order)
