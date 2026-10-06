"""Tests for the pure folder-upload planning logic in tasks/upload.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openwopan.tasks.upload import (
    JUNK_FILE_NAMES,
    UploadBatchSummary,
    find_upload_conflicts,
    format_upload_summary,
    next_available_name,
    resolve_upload_targets,
    scan_folder_tree,
    scan_upload_inputs,
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


def test_scan_folder_tree_rejects_symlink_root(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    _write(real_dir / "inner.txt")
    linked_root = tmp_path / "linked-root"
    try:
        os.symlink(real_dir, linked_root)
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create symlinks")

    with pytest.raises(ValueError, match="不能上传符号链接"):
        scan_folder_tree(linked_root)


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
        ("report.txt", {"report.txt"}, "report (copy).txt"),
        (
            "report.txt",
            {"report.txt", "report (copy).txt"},
            "report (copy) (copy).txt",
        ),
        ("photos", {"photos"}, "photos (copy)"),
        (
            "photos",
            {"photos", "photos (copy)", "photos (copy) (copy)"},
            "photos (copy) (copy) (copy)",
        ),
        ("archive.tar.gz", {"archive.tar.gz"}, "archive.tar (copy).gz"),
        (
            "报告.txt",
            {"报告.txt", "报告 (copy).txt"},
            "报告 (copy) (copy).txt",
        ),
    ],
)
def test_next_available_name_follows_duplicate_counter_format(
    requested: str, used: set[str], expected: str
) -> None:
    assert next_available_name(requested, used) == expected


def test_resolve_upload_targets_skips_only_conflicting_paths(tmp_path: Path) -> None:
    first = _write(tmp_path / "same.txt", b"one")
    second = _write(tmp_path / "other.txt", b"two")
    duplicate = _write(tmp_path / "elsewhere" / "same.txt", b"three")
    paths = (first, second, duplicate)

    assert find_upload_conflicts(paths, {"same.txt"}) == (first, duplicate)
    targets = resolve_upload_targets(paths, {"same.txt"}, "skip")

    assert [(target.local_path, target.upload_name) for target in targets] == [
        (second, None),
    ]


def test_resolve_upload_targets_appends_copy_for_repeated_conflicts(tmp_path: Path) -> None:
    first = _write(tmp_path / "report.txt", b"one")
    second = _write(tmp_path / "other" / "report.txt", b"two")
    third = _write(tmp_path / "third" / "report.txt", b"three")

    targets = resolve_upload_targets(
        (first, second, third),
        {"report.txt", "report (copy).txt"},
        "copy",
    )

    assert [(target.local_path, target.upload_name) for target in targets] == [
        (first, "report (copy) (copy).txt"),
        (second, "report (copy) (copy) (copy).txt"),
        (third, "report (copy) (copy) (copy) (copy).txt"),
    ]


def test_resolve_upload_targets_merge_keeps_original_names(tmp_path: Path) -> None:
    """合并策略：冲突项保留原名（文件夹据此续传合并），不再改副本名。"""
    folder = tmp_path / "已看完"
    folder.mkdir()
    conflicting_file = _write(tmp_path / "same.txt", b"one")
    fresh_file = _write(tmp_path / "new.txt", b"two")

    targets = resolve_upload_targets(
        (folder, conflicting_file, fresh_file), {"已看完", "same.txt"}, "merge"
    )

    assert [(target.local_path, target.upload_name) for target in targets] == [
        (folder, None),
        (conflicting_file, None),
        (fresh_file, None),
    ]


def test_resolve_upload_targets_rejects_unknown_resolution(tmp_path: Path) -> None:
    path = _write(tmp_path / "report.txt")

    with pytest.raises(ValueError, match="不支持的上传冲突策略"):
        resolve_upload_targets((path,), {"report.txt"}, "invalid")  # type: ignore[arg-type]


def test_scan_upload_inputs_deduplicates_and_bounds_preview(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root / "a.txt", b"123")
    _write(root / "b.txt", b"1234")
    direct = _write(tmp_path / "direct.txt", b"12")

    summary = scan_upload_inputs((root, root, direct), preview_limit=2)

    assert isinstance(summary, UploadBatchSummary)
    assert summary.top_paths == (root, direct)
    assert summary.file_count == 3
    assert summary.folder_count == 1
    assert summary.total_bytes == 9
    assert len(summary.preview) == 2
    assert summary.omitted_count == 2
    assert "文件 3 个" in format_upload_summary(summary)


def test_scan_upload_inputs_reserves_preview_for_top_level_items(tmp_path: Path) -> None:
    root = tmp_path / "folder"
    root.mkdir()
    for index in range(25):
        _write(root / f"{index:02d}.txt", b"x")
    direct = _write(tmp_path / "duplicate.txt", b"x")

    summary = scan_upload_inputs((root, direct))

    assert summary.top_paths == (root, direct)
    assert len(summary.preview) == 20
    assert [entry.local_path for entry in summary.preview[:2]] == [root, direct]
    assert summary.omitted_count == 7


def test_scan_upload_inputs_skips_top_level_junk_and_symlink(tmp_path: Path) -> None:
    junk = _write(tmp_path / ".DS_Store", b"junk")
    target = _write(tmp_path / "target.txt", b"target")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    summary = scan_upload_inputs((junk, link, target))

    assert summary.top_paths == (target,)
    assert summary.file_count == 1
    assert [entry.local_path for entry in summary.preview] == [target]


def test_scan_upload_inputs_reports_empty_folder_without_file_tasks(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    summary = scan_upload_inputs((empty,))

    assert summary.file_count == 0
    assert summary.folder_count == 1
    assert summary.total_bytes == 0
    assert summary.omitted_count == 0
