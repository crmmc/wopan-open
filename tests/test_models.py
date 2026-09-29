import importlib
from importlib.metadata import PackageNotFoundError, version

import pytest

from openwopan import __version__
from openwopan.wopan.models import (
    WopanCloudUsage,
    WopanItem,
    WopanItemKind,
    WopanRecycleItem,
)


def test_package_version_is_available() -> None:
    assert isinstance(__version__, str)


def test_package_version_falls_back_when_distribution_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """importlib.metadata 查不到 openwopan 发行版时，__version__ 应回退为 "0.0.0"。"""
    import openwopan

    def raise_package_not_found(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", raise_package_not_found)
    try:
        module = importlib.reload(openwopan)
        assert module.__version__ == "0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(openwopan)

    assert openwopan.__version__ == version("openwopan")


def test_wopan_item_model_uses_openwopan_fields() -> None:
    item = WopanItem(
        item_id="root",
        name="Root",
        kind=WopanItemKind.FOLDER,
        file_type="0",
        download_id="fid-root",
    )

    assert item.item_id == "root"
    assert item.name == "Root"
    assert item.kind is WopanItemKind.FOLDER
    assert item.file_type == "0"
    assert item.download_id == "fid-root"


def test_wopan_item_rejects_negative_size() -> None:
    with pytest.raises(ValueError, match="size must be non-negative"):
        WopanItem(item_id="bad", name="Bad", kind=WopanItemKind.FILE, size=-1)


def test_wopan_item_requires_id_and_name() -> None:
    with pytest.raises(ValueError, match="item_id"):
        WopanItem(item_id="", name="Bad", kind=WopanItemKind.FILE)

    with pytest.raises(ValueError, match="name"):
        WopanItem(item_id="bad", name="", kind=WopanItemKind.FILE)

    with pytest.raises(ValueError, match="download_id"):
        WopanItem(item_id="bad", name="Bad", kind=WopanItemKind.FILE, download_id="")


def test_wopan_cloud_usage_validates_byte_counts() -> None:
    usage = WopanCloudUsage(used_bytes=1024, total_bytes=2048, vip_level="3")

    assert usage.used_bytes == 1024
    assert usage.total_bytes == 2048
    assert usage.vip_level == "3"

    with pytest.raises(ValueError, match="used_bytes"):
        WopanCloudUsage(used_bytes=-1, total_bytes=2048)

    with pytest.raises(ValueError, match="total_bytes"):
        WopanCloudUsage(used_bytes=0, total_bytes=0)


def test_wopan_recycle_item_uses_openwopan_fields() -> None:
    item = WopanRecycleItem(
        delete_no="d-1",
        item_id="item-1",
        name="report.txt",
        kind=WopanItemKind.FILE,
        size=2048,
        keep_days=30,
        file_type="4",
    )

    assert item.delete_no == "d-1"
    assert item.item_id == "item-1"
    assert item.kind is WopanItemKind.FILE
    assert item.size == 2048
    assert item.keep_days == 30
    assert item.file_type == "4"


def test_wopan_recycle_item_requires_delete_no_and_name() -> None:
    with pytest.raises(ValueError, match="delete_no"):
        WopanRecycleItem(delete_no="", item_id="i", name="n", kind=WopanItemKind.FILE)

    with pytest.raises(ValueError, match="name"):
        WopanRecycleItem(delete_no="d", item_id="i", name="", kind=WopanItemKind.FILE)


def test_wopan_recycle_item_rejects_negative_size_and_keep_days() -> None:
    with pytest.raises(ValueError, match="size"):
        WopanRecycleItem(
            delete_no="d", item_id="i", name="n", kind=WopanItemKind.FILE, size=-1
        )

    with pytest.raises(ValueError, match="keep_days"):
        WopanRecycleItem(
            delete_no="d", item_id="i", name="n", kind=WopanItemKind.FILE, keep_days=-1
        )
