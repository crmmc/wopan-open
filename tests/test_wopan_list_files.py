from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan.client import WopanClient
from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanResponseError,
)
from openwopan.wopan.models import WopanItemKind

TOKEN = "1234567890abcdef-token"
COOKIE_HEADER = f"foo=bar; WoCloud-Web-Token={TOKEN}"


def _json_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _pkcs7_pad(data: bytes, block_size: int) -> bytes:
    padding = block_size - len(data) % block_size
    return data + bytes([padding]) * padding


def _pkcs7_unpad(data: bytes) -> bytes:
    padding = data[-1]
    return data[:-padding]


def _encrypt_wohome_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    cipher = Cipher(algorithms.AES(TOKEN[:16].encode()), modes.CBC(b"wNSOYIB1k1DjY5lA"))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(_pkcs7_pad(encoded, 16)) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _decrypt_wohome_payload(payload: str) -> dict[str, object]:
    cipher = Cipher(algorithms.AES(TOKEN[:16].encode()), modes.CBC(b"wNSOYIB1k1DjY5lA"))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(base64.b64decode(payload)) + decryptor.finalize()
    decoded = json.loads(_pkcs7_unpad(decrypted).decode())
    assert isinstance(decoded, dict)
    return decoded


def test_list_files_maps_query_all_files_response_to_openwopan_items() -> None:
    captured_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = json.loads(request.content)
        assert request.url.path == "/wohome/dispatcher"
        assert request.headers["Accesstoken"] == TOKEN
        assert captured_body["header"]["key"] == "QueryAllFiles"
        assert captured_body["header"]["channel"] == "wohome"
        return _json_response(
            {
                "STATUS": "200",
                "MSG": "ok",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": _encrypt_wohome_payload(
                        {
                            "systemDirs": [
                                {
                                    "id": "system-1",
                                    "name": "图片",
                                    "type": "0",
                                    "parentDirectoryId": "0",
                                    "size": 0,
                                    "createTime": "20260626010203",
                                }
                            ],
                            "files": [
                                {
                                    "id": "file-1",
                                    "fid": "fid-1",
                                    "name": "report.txt",
                                    "type": "1",
                                    "fileType": "4",
                                    "parentDirectoryId": "0",
                                    "size": "2048",
                                    "createTime": "20260626112233",
                                }
                            ],
                        }
                    ),
                },
            }
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    items = client.list_files("0")

    param = _decrypt_wohome_payload(captured_body["body"]["param"])  # type: ignore[index]
    assert param["parentDirectoryId"] == "0"
    assert param["pageNum"] == 0
    assert param["pageSize"] == 100
    assert [item.name for item in items] == ["图片", "report.txt"]
    assert items[0].kind is WopanItemKind.FOLDER
    assert items[1].kind is WopanItemKind.FILE
    assert items[1].file_type == "4"
    assert items[1].download_id == "fid-1"
    assert items[1].size == 2048
    assert items[1].updated_at is not None
    assert items[1].updated_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-06-26 11:22:33"


def test_list_files_maps_login_expired() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "1001",
                    "RSP_DESC": "login expired",
                    "DATA": "",
                },
            }
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanAuthenticationError, match="login expired"):
        client.list_files("0")


def test_list_files_skips_items_with_unknown_type(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": _encrypt_wohome_payload(
                        {
                            "files": [
                                {"id": "item-1", "name": "bad", "type": ""},
                                {"id": "item-2", "name": "report.txt", "type": "1"},
                                {"id": "item-3", "name": "unknown", "type": "9"},
                            ]
                        }
                    ),
                },
            }
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level("WARNING", logger="openwopan.wopan.client"):
        items = client.list_files("0")

    assert [item.item_id for item in items] == ["item-2"]
    assert "wopan.list_files.skip_item_unknown_type" in caplog.text
    assert "item-1" in caplog.text
    assert "item-3" in caplog.text

@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"id": "", "name": "missing-id", "type": "1"}, "missing id"),
        ({"id": "item-1", "name": "", "type": "1"}, "missing name"),
    ],
)
def test_list_files_rejects_items_missing_required_fields(
    payload: dict[str, object],
    message: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": _encrypt_wohome_payload({"files": [payload]}),
                },
            }
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanResponseError, match=message):
        client.list_files("0")


def test_list_files_requires_parent_id() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=transport),
    )

    with pytest.raises(ValueError, match="parent_id"):
        client.list_files("")


def test_client_requires_token_cookie_for_file_browser_session() -> None:
    with pytest.raises(WopanAuthenticationError, match="WoCloud-Web-Token"):
        WopanClient("foo=bar")
