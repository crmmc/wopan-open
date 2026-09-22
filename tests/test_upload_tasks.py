"""Tests for the pure folder-upload planning logic in tasks/upload.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openwopan.tasks.upload import (
    JUNK_FILE_NAMES,
    next_available_name,
    scan_folder_tree,
)


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_scan_folder_tree_keeps_empty_dirs_and_orders_folders_parent_first(
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    _write(root / "top.txt", b"12345")
    _write(root / "相册" / "2024" / "春节.md", b"hello")
    (root / "空目录").mkdir()

    plan = scan_folder_tree(root)

    assert plan.root_name == "photos"
    assert plan.folders == ("相册", "相册/2024", "空目录")
    assert [file.rel_dir for file in plan.files] == ["", "相册/2024"]
    assert [(file.name, file.size) for file in plan.files] == [
        ("top.txt", 5),
        ("春节.md", 5),
    ]
    assert all(file.local_path.is_file() for file in plan.files)


def test_scan_folder_tree_skips_junk_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for junk_name in JUNK_FILE_NAMES:
        _write(root / junk_name)
    _write(root / "keep.txt")

    plan = scan_folder_tree(root)

    assert [file.name for file in plan.files] == ["keep.txt"]


def test_scan_folder_tree_skips_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root / "real.txt")
    linked_dir = tmp_path / "linked-dir"
    linked_dir.mkdir()
    _write(linked_dir / "inner.txt")
    try:
        os.symlink(linked_dir, root / "linked-dir")
        os.symlink(root / "real.txt", root / "linked-file.txt")
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create symlinks")

    plan = scan_folder_tree(root)

    assert plan.folders == ()
    assert [file.name for file in plan.files] == ["real.txt"]


def test_scan_folder_tree_reports_unreadable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "locked").mkdir()
    real_scandir = os.scandir

    def failing_scandir(path: object):
        if Path(str(path)).name == "locked":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", failing_scandir)
    with pytest.raises(OSError) as excinfo:
        scan_folder_tree(root)

    assert str(root / "locked") in str(excinfo.value)


@pytest.mark.parametrize(
    ("local_path_name", "match"),
    [
        ("missing-folder", "无法读取文件夹"),
        ("plain-file.txt", "无法读取文件夹"),
    ],
)
def test_scan_folder_tree_rejects_non_directory(
    tmp_path: Path, local_path_name: str, match: str
) -> None:
    plain_file = _write(tmp_path / "plain-file.txt")

    target = tmp_path / "missing-folder" if local_path_name == "missing-folder" else plain_file
    with pytest.raises(OSError, match=match):
        scan_folder_tree(target)


@pytest.mark.parametrize(
    ("requested", "used", "expected"),
    [
        ("report.txt", set(), "report.txt"),
        ("report.txt", {"无关.txt"}, "report.txt"),
        ("report.txt", {"report.txt"}, "report (1).txt"),
        ("report.txt", {"report.txt", "report (1).txt"}, "report (2).txt"),
        ("photos", {"photos"}, "photos (1)"),
        ("photos", {"photos", "photos (1)", "photos (2)"}, "photos (3)"),
        ("archive.tar.gz", {"archive.tar.gz"}, "archive.tar (1).gz"),
        ("报告.txt", {"报告.txt", "报告 (1).txt"}, "报告 (2).txt"),
    ],
)
def test_next_available_name_follows_duplicate_counter_format(
    requested: str, used: set[str], expected: str
) -> None:
    assert next_available_name(requested, used) == expected
