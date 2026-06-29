import pytest

from openwopan import __version__
from openwopan.wopan.models import WopanCloudUsage, WopanItem, WopanItemKind


def test_package_version_is_available() -> None:
    assert isinstance(__version__, str)


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
