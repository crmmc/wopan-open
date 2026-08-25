from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan import client as client_module
from openwopan.wopan.client import WopanClient
from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanResponseError,
)
from openwopan.wopan.models import WopanItemKind

TOKEN = "1234567890abcdef-token"
COOKIE_HEADER = f"foo=bar; WoCloud-Web-Token={TOKEN}"
IV = b"wNSOYIB1k1DjY5lA"


def _pkcs7_pad(data: bytes, block_size: int) -> bytes:
    padding = block_size - len(data) % block_size
    return data + bytes([padding]) * padding


def _encrypt_payload(payload: object, key: bytes) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    cipher = Cipher(algorithms.AES(key), modes.CBC(IV))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(_pkcs7_pad(encoded, 16)) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _success_response(data: object) -> httpx.Response:
    if isinstance(data, str):
        response_data: object = data
    elif isinstance(data, (dict, list)):
        response_data = _encrypt_payload(data, TOKEN[:16].encode())
    else:  # pragma: no cover - defensive
        response_data = data
    return httpx.Response(
        200,
        json={
            "STATUS": "200",
            "MSG": "ok",
            "RSP": {"RSP_CODE": "0000", "RSP_DESC": "success", "DATA": response_data},
        },
    )


def _upload_client(handler) -> WopanClient:
    return WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _upload_handler(
    upload_response: httpx.Response | Exception,
    zone_url: str = "https://upload.example.test",
):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": zone_url})
        if isinstance(upload_response, Exception):
            raise upload_response
        return upload_response

    return handler


# -- argument validation ------------------------------------------------------


def test_validate_session_rejects_empty_token() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500))
    wopan = WopanClient(COOKIE_HEADER, http_client=httpx.Client(transport=transport))

    with pytest.raises(ValueError, match="token must not be empty"):
        wopan.validate_session("")


def test_query_cloud_usage_rejects_empty_account_id() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500))
    wopan = WopanClient(COOKIE_HEADER, http_client=httpx.Client(transport=transport))

    with pytest.raises(ValueError, match="account_id must not be empty"):
        wopan.query_cloud_usage("")


def test_client_rejects_empty_cookie_header() -> None:
    with pytest.raises(ValueError, match="cookie_header must not be empty"):
        WopanClient("")


def test_upload_file_rejects_missing_local_file(tmp_path: Path) -> None:
    wopan = _upload_client(_upload_handler(httpx.Response(200, json={})))

    with pytest.raises(ValueError, match="existing file"):
        wopan.upload_file("0", tmp_path / "missing.bin")


def test_upload_file_rejects_directory(tmp_path: Path) -> None:
    wopan = _upload_client(_upload_handler(httpx.Response(200, json={})))

    with pytest.raises(ValueError, match="existing file"):
        wopan.upload_file("0", tmp_path)


# -- upload partial/overall failure semantics ---------------------------------


def test_upload_part_retries_transient_business_error_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部分失败语义：单片先失败、重试后成功，整体上传成功。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"abcdefghijklmnopq")
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        content_type = request.headers["Content-Type"]
        body = request.content
        part_marker = b'name="partIndex"'
        index_start = body.find(part_marker) + len(part_marker)
        index_end = body.find(b"-", index_start)
        part_index = int(body[index_start:index_end].strip(b"\r\n").strip(b'"'))
        attempts.append(part_index)
        if part_index == 2 and attempts.count(2) == 1:
            return httpx.Response(200, json={"code": "9999", "msg": "busy"})
        return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}})

    item = _upload_client(handler).upload_file(
        "folder-1", local_file, max_upload_threads=2, retry_max_attempts=1
    )

    assert item.item_id == "fid-1"
    assert attempts.count(2) == 2  # part 2 retried once and then succeeded


def test_upload_part_raises_after_retry_exhaustion(tmp_path: Path) -> None:
    """整体失败语义：单片重试耗尽后整体上传失败。"""
    from openwopan.wopan.errors import WopanBusinessError

    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")

    with pytest.raises(WopanBusinessError, match="busy"):
        _upload_client(_upload_handler(httpx.Response(200, json={"code": "9999", "msg": "busy"}))).upload_file(
            "0", local_file, retry_max_attempts=0
        )


def test_upload_file_reraises_http_error(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")

    with pytest.raises(httpx.HTTPStatusError):
        _upload_client(_upload_handler(httpx.Response(500))).upload_file(
            "0", local_file, retry_max_attempts=0
        )


def test_upload_file_maps_local_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")

    def failing_read(self: Path) -> bytes:
        raise OSError("disk error")

    monkeypatch.setattr(Path, "read_bytes", failing_read)

    with pytest.raises(WopanResponseError, match="cannot be decoded"):
        _upload_client(_upload_handler(httpx.Response(200, json={}))).upload_file(
            "0", local_file, retry_max_attempts=0
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"code": "0000", "data": []}, "data is not an object"),
        ({"code": "0000", "data": {}}, "missing fid"),
    ],
)
def test_upload_file_rejects_malformed_success_payload(
    tmp_path: Path, payload: dict[str, object], match: str
) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")

    with pytest.raises(WopanResponseError, match=match):
        _upload_client(_upload_handler(httpx.Response(200, json=payload))).upload_file(
            "0", local_file, retry_max_attempts=0
        )


def test_upload_parts_parallel_requires_at_least_one_part() -> None:
    wopan = _upload_client(_upload_handler(httpx.Response(200, json={})))

    with pytest.raises(WopanResponseError, match="no response"):
        wopan._upload_parts_parallel(
            "https://upload.example.test/openapi/client/upload2C",
            {},
            "f.bin",
            "application/octet-stream",
            Path("f.bin"),
            part_size=1,
            total_parts=0,
            max_workers=1,
            max_attempts=1,
        )


def test_upload_part_fails_without_attempts() -> None:
    wopan = _upload_client(_upload_handler(httpx.Response(200, json={})))

    with pytest.raises(WopanResponseError, match="upload part failed"):
        wopan._upload_part(
            "https://upload.example.test/openapi/client/upload2C",
            {},
            "f.bin",
            "application/octet-stream",
            b"data",
            part_index=1,
            max_attempts=0,
        )


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[1, 2]", "response is not an object"),
        ("not-json", "cannot be decoded"),
    ],
)
def test_upload_part_rejects_malformed_response(tmp_path: Path, body: str, match: str) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")
    response = httpx.Response(200, content=body.encode())

    with pytest.raises(WopanResponseError, match=match):
        _upload_client(_upload_handler(response)).upload_file(
            "0", local_file, retry_max_attempts=0
        )


def test_get_download_info_rejects_non_object_entries() -> None:
    wopan = _upload_client(_upload_handler(httpx.Response(200, json={})))

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/wohome/dispatcher")
        return _success_response([42])

    wopan = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(WopanResponseError, match="not an object"):
        wopan.get_download_info("file-1")


# -- dispatch payload edge cases -----------------------------------------------


def _dispatch_client(handler) -> WopanClient:
    return WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_validate_session_rejects_non_object_data() -> None:
    wopan = _dispatch_client(
        lambda _r: httpx.Response(
            200,
            json={
                "STATUS": "200",
                "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": [1]},
            },
        )
    )

    with pytest.raises(WopanResponseError, match="DATA is not an object"):
        wopan.validate_session(TOKEN)


def test_validate_session_rejects_missing_user_id() -> None:
    wopan = _dispatch_client(
        lambda _r: httpx.Response(
            200,
            json={
                "STATUS": "200",
                "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": {}},
            },
        )
    )

    with pytest.raises(WopanResponseError, match="missing userId"):
        wopan.validate_session(TOKEN)


def test_dispatch_maps_non_200_status_without_message() -> None:
    wopan = _dispatch_client(
        lambda _r: httpx.Response(200, json={"STATUS": "500", "RSP": {}})
    )

    with pytest.raises(WopanResponseError, match="WoPan service call failed"):
        wopan.query_cloud_usage("13800138000")


def test_dispatch_returns_empty_object_for_empty_data() -> None:
    wopan = _dispatch_client(
        lambda _r: httpx.Response(
            200,
            json={
                "STATUS": "200",
                "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": ""},
            },
        )
    )

    with pytest.raises(WopanResponseError, match="usageInfo"):
        wopan.query_cloud_usage("13800138000")  # DATA={} parses, business check fails


def test_dispatch_rejects_undecodable_encrypted_data() -> None:
    wopan = _dispatch_client(
        lambda _r: httpx.Response(
            200,
            json={
                "STATUS": "200",
                "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": "%%%not-base64%%%"},
            },
        )
    )

    with pytest.raises(WopanResponseError, match="cannot be decoded"):
        wopan.query_cloud_usage("13800138000")


def test_dispatch_rejects_non_object_decrypted_data() -> None:
    wopan = _dispatch_client(
        lambda _r: httpx.Response(
            200,
            json={
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "ok",
                    "DATA": _encrypt_payload("plain string", TOKEN[:16].encode()),
                },
            },
        )
    )

    with pytest.raises(WopanResponseError, match="cannot be decoded"):
        wopan.query_cloud_usage("13800138000")


def test_dispatch_reraises_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    wopan = _dispatch_client(handler)

    with pytest.raises(httpx.HTTPError):
        wopan.query_cloud_usage("13800138000")


# -- list_files item parsing edge cases ----------------------------------------


def _list_files_client(data: object) -> WopanClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/wohome/dispatcher")
        return _success_response(data)

    return _dispatch_client(handler)


def test_list_files_skips_unknown_item_types() -> None:
    wopan = _list_files_client(
        {
            "systemDirs": None,
            "files": [
                {"id": "f1", "name": "a.txt", "type": "1", "size": 3},
                {"id": "f2", "name": "weird", "type": "7"},
            ],
        }
    )

    items = wopan.list_files("0")

    assert [item.item_id for item in items] == ["f1"]
    assert items[0].size == 3


@pytest.mark.parametrize(
    "data",
    [
        {"files": "oops"},
        {"files": [42]},
    ],
    ids=["field-not-list", "item-not-object"],
)
def test_list_files_rejects_malformed_payloads(data: object) -> None:
    wopan = _list_files_client(data)

    with pytest.raises(WopanResponseError):
        wopan.list_files("0")


def test_list_files_rejects_invalid_timestamp() -> None:
    wopan = _list_files_client(
        {"files": [{"id": "f1", "name": "a", "type": "1", "updateTime": "not-a-date"}]}
    )

    with pytest.raises(WopanResponseError, match="invalid"):
        wopan.list_files("0")


# -- pure helpers (table-driven) ------------------------------------------------


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, 5, 5),
        (1.5, 5, 5),
        ("abc", 5, 5),
        ("3", 5, 3),
        (2, 5, 2),
        (100, 5, 5),  # clamped to max
        (-4, 5, 1),  # clamped to min
    ],
)
def test_bounded_int(value: object, default: int, expected: int) -> None:
    assert client_module._bounded_int(value, default, 1, 5) == expected


def test_read_dispatch_data_requires_object() -> None:
    raw = {
        "STATUS": "200",
        "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": {"a": 1}},
    }
    assert client_module._read_dispatch_data(raw, "k" * 16) == {"a": 1}

    raw_list = {
        "STATUS": "200",
        "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": [1]},
    }
    with pytest.raises(WopanResponseError, match="DATA is not an object"):
        client_module._read_dispatch_data(raw_list, "k" * 16)


@pytest.mark.parametrize(
    "cookie_header",
    [
        "foo=bar",
        'WoCloud-Web-Token=""',
        "WoCloud-Web-Token=%22%22",
    ],
)
def test_extract_token_rejects_missing_or_empty(cookie_header: str) -> None:
    with pytest.raises(WopanAuthenticationError):
        client_module._extract_token_from_cookie_header(cookie_header)


def test_wohome_crypto_key_requires_long_token() -> None:
    with pytest.raises(WopanAuthenticationError, match="too short"):
        client_module._wohome_crypto_key("short")
    assert client_module._wohome_crypto_key("1234567890abcdef") == "1234567890abcdef"


def test_read_wopan_item_rejects_unknown_type() -> None:
    with pytest.raises(WopanResponseError, match="unknown type"):
        client_module._read_wopan_item({"id": "1", "name": "n", "type": "9"}, "0")


def test_read_wopan_item_type_handles_missing_type() -> None:
    assert client_module._read_wopan_item_type({}) == ""
    assert client_module._read_wopan_item_type({"type": 1}) == "1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("12", 12),
    ],
)
def test_read_optional_int_valid(value: object, expected: int | None) -> None:
    assert client_module._read_optional_int(value) == expected


@pytest.mark.parametrize(
    "value",
    ["abc", [], -1],
)
def test_read_optional_int_rejects_invalid(value: object) -> None:
    with pytest.raises(WopanResponseError):
        client_module._read_optional_int(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("vip", "vip")],
)
def test_read_optional_text(value: object, expected: str | None) -> None:
    assert client_module._read_optional_text(value) == expected


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (None, "missing"),
        ("", "missing"),
        ("abc", "not an integer"),
        (None, "missing"),
    ],
)
def test_read_required_int_rejects_invalid(value: object, match: str) -> None:
    with pytest.raises(WopanResponseError, match=match):
        client_module._read_required_int(value, "field")


def test_read_required_non_negative_and_positive() -> None:
    assert client_module._read_required_non_negative_int(0, "f") == 0
    with pytest.raises(WopanResponseError, match="negative"):
        client_module._read_required_non_negative_int(-1, "f")
    assert client_module._read_required_positive_int(5, "f") == 5
    with pytest.raises(WopanResponseError, match="positive"):
        client_module._read_required_positive_int(0, "f")


def test_wopan_kind_value() -> None:
    assert client_module._wopan_kind_value(WopanItemKind.FOLDER) == 0
    assert client_module._wopan_kind_value(WopanItemKind.FILE) == 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("photo.JPG", "1"),
        ("clip.mp4", "2"),
        ("song.mp3", "3"),
        ("doc.txt", "4"),
        ("archive.zip", "0"),
        ("noext", "0"),
    ],
)
def test_guess_upload_file_type(name: str, expected: str) -> None:
    assert client_module._guess_upload_file_type(name) == expected


def test_read_wopan_timestamp_falls_back_through_fields() -> None:
    parsed = client_module._read_wopan_timestamp({"modifyTime": "20240102030405"})
    assert parsed == datetime(2024, 1, 2, 3, 4, 5)
    assert client_module._read_wopan_timestamp({"createTime": ""}) is None
    with pytest.raises(WopanResponseError, match="invalid"):
        client_module._read_wopan_timestamp({"createTime": "bad"})


@pytest.mark.parametrize(
    "data",
    [b"", b"\x00" * 8, b"\x01\x02\x03"],
)
def test_pkcs7_unpad_rejects_invalid_padding(data: bytes) -> None:
    with pytest.raises(WopanResponseError, match="padding"):
        client_module._pkcs7_unpad(data)


def test_pkcs7_unpad_accepts_valid_padding() -> None:
    assert client_module._pkcs7_unpad(b"ab\x02\x02") == b"ab"


def test_dispatch_wohome_rejects_non_object_data() -> None:
    wopan = _dispatch_client(lambda _r: _success_response([1, 2]))

    with pytest.raises(WopanResponseError, match="DATA is not an object"):
        wopan.query_cloud_usage("13800138000")


def test_dispatch_rejects_non_object_response_body() -> None:
    wopan = _dispatch_client(lambda _r: httpx.Response(200, json=[1, 2]))

    with pytest.raises(WopanResponseError, match="response is not an object"):
        wopan.query_cloud_usage("13800138000")


def test_dispatch_rejects_missing_rsp() -> None:
    wopan = _dispatch_client(lambda _r: httpx.Response(200, json={"STATUS": "200"}))

    with pytest.raises(WopanResponseError, match="missing RSP"):
        wopan.query_cloud_usage("13800138000")
