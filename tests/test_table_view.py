"""Unit tests for the shared view-layer table sort helpers (10-01-table-sort-filter)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHeaderView

from openwopan.ui.table_view import (
    TableSortState,
    cycle_sort_state,
    sorted_view,
    sync_sort_indicator,
)


def test_cycle_advances_none_asc_desc_none() -> None:
    state = TableSortState()

    state = cycle_sort_state(state, 1)
    assert state == TableSortState(column=1, order=Qt.SortOrder.AscendingOrder)
    state = cycle_sort_state(state, 1)
    assert state == TableSortState(column=1, order=Qt.SortOrder.DescendingOrder)
    state = cycle_sort_state(state, 1)
    assert state == TableSortState()


def test_cycle_switching_column_resets_to_ascending() -> None:
    state = TableSortState(column=1, order=Qt.SortOrder.DescendingOrder)

    state = cycle_sort_state(state, 2)

    assert state == TableSortState(column=2, order=Qt.SortOrder.AscendingOrder)


def test_sorted_view_without_column_returns_copy_in_source_order() -> None:
    entries = [3, 1, 2]

    result = sorted_view(entries, TableSortState(), {0: lambda value: value})

    assert result == [3, 1, 2]
    assert result is not entries
    assert entries == [3, 1, 2]


def test_sorted_view_ignores_column_without_key_function() -> None:
    entries = [3, 1, 2]

    result = sorted_view(
        entries,
        TableSortState(column=7, order=Qt.SortOrder.AscendingOrder),
        {0: lambda value: value},
    )

    assert result == [3, 1, 2]


def test_sorted_view_sorts_ascending_and_descending() -> None:
    entries = [3, 1, 2]

    ascending = sorted_view(
        entries, TableSortState(column=0, order=Qt.SortOrder.AscendingOrder), {0: str}
    )
    descending = sorted_view(
        entries, TableSortState(column=0, order=Qt.SortOrder.DescendingOrder), {0: str}
    )

    assert ascending == [1, 2, 3]
    assert descending == [3, 2, 1]
    assert entries == [3, 1, 2]


def test_sorted_view_is_stable_for_equal_keys() -> None:
    entries = [("a", 1), ("b", 0), ("c", 1), ("d", 0)]

    ascending = sorted_view(
        entries, TableSortState(column=0, order=Qt.SortOrder.AscendingOrder), {0: lambda e: e[1]}
    )
    descending = sorted_view(
        entries, TableSortState(column=0, order=Qt.SortOrder.DescendingOrder), {0: lambda e: e[1]}
    )

    # sorted(reverse=True) is stable: equal keys keep their relative order in
    # both directions, so coalesced progress renders never reshuffle rows.
    assert ascending == [("b", 0), ("d", 0), ("a", 1), ("c", 1)]
    assert descending == [("a", 1), ("c", 1), ("b", 0), ("d", 0)]


def test_sync_sort_indicator_mirrors_state(qapp: QApplication) -> None:
    header = QHeaderView(Qt.Orientation.Horizontal)

    sync_sort_indicator(header, TableSortState(column=2, order=Qt.SortOrder.DescendingOrder))

    assert header.sortIndicatorSection() == 2
    assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder


def test_sync_sort_indicator_clears_when_inactive(qapp: QApplication) -> None:
    header = QHeaderView(Qt.Orientation.Horizontal)
    sync_sort_indicator(header, TableSortState(column=2, order=Qt.SortOrder.AscendingOrder))

    sync_sort_indicator(header, TableSortState())

    assert header.sortIndicatorSection() == -1


def test_sync_sort_indicator_tolerates_missing_header() -> None:
    # Defensive None guard: horizontalHeader() is typed optional.
    sync_sort_indicator(None, TableSortState(column=1, order=Qt.SortOrder.AscendingOrder))
