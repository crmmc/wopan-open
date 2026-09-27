from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan import client as client_module
from openwopan.wopan.client import WopanClient
from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanBusinessError,
    WopanResponseError,
    WopanUploadCancelledError,
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


def test_upload_file_reports_completed_progress(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")
    progress: list[tuple[int, int]] = []

    item = _upload_client(
        _upload_handler(httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}}))
    ).upload_file(
        "0",
        local_file,
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert item.item_id == "fid-1"
    assert progress == [(7, 7)]


    """整体失败语义：单片重试耗尽后整体上传失败。"""
    from openwopan.wopan.errors import WopanBusinessError

    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")

    with pytest.raises(WopanBusinessError, match="busy"):
        _upload_client(
            _upload_handler(httpx.Response(200, json={"code": "9999", "msg": "busy"}))
        ).upload_file("0", local_file, retry_max_attempts=0)


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


def _decrypt_payload(encrypted: str, key: bytes) -> object:
    cipher = Cipher(algorithms.AES(key), modes.CBC(IV))
    decryptor = cipher.decryptor()
    padded = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
    return json.loads(padded[: -padded[-1]])


def _capture_upload_body() -> tuple[dict[str, object], dict[str, object]]:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        captured["body"] = request.content
        return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}})

    return captured, handler


def _parse_multipart_fields(body: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for block in body.split(b"form-data; name=")[1:]:
        name_end = block.find(b"\r\n\r\n")
        value_end = block.find(b"\r\n--")
        name = block[:name_end].strip(b'"').decode()
        value = block[name_end + 4 : value_end].decode()
        fields[name] = value
    return fields


def test_upload_file_uses_upload_name_for_metadata(tmp_path: Path) -> None:
    """upload_name 覆盖本地名：fileName/fileInfo(fileType)/mime 均基于该名。"""
    local_file = tmp_path / "local-name.txt"
    local_file.write_bytes(b"content")
    captured, handler = _capture_upload_body()

    item = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ).upload_file("folder-1", local_file, retry_max_attempts=0, upload_name="cloud.png")

    body = cast(bytes, captured["body"])
    fields = _parse_multipart_fields(body)
    file_info = _decrypt_payload(fields["fileInfo"], TOKEN[:16].encode())
    assert item.name == "cloud.png"
    assert item.file_type == "1"
    assert fields["fileName"] == "cloud.png"
    assert file_info == {
        "spaceType": "0",
        "directoryId": "folder-1",
        "batchNo": file_info["batchNo"],
        "fileName": "cloud.png",
        "fileSize": len(b"content"),
        "fileType": "1",
    }
    assert b'filename="cloud.png"' in body
    assert b"image/png" in body


def test_upload_file_keeps_local_name_without_upload_name(tmp_path: Path) -> None:
    """缺省 upload_name 时沿用本地文件名（现有调用零影响）。"""
    local_file = tmp_path / "local-name.txt"
    local_file.write_bytes(b"content")
    captured, handler = _capture_upload_body()

    item = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ).upload_file("folder-1", local_file, retry_max_attempts=0)

    body = cast(bytes, captured["body"])
    fields = _parse_multipart_fields(body)
    assert item.name == "local-name.txt"
    assert fields["fileName"] == "local-name.txt"
    assert b'filename="local-name.txt"' in body
    assert b"text/plain" in body


# -- resumable upload (UploadResumeContext) ------------------------------------


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    start = body.find(marker) + len(marker)
    value_start = body.find(b"\r\n\r\n", start) + 4
    value_end = body.find(b"\r\n", value_start)
    return body[value_start:value_end].decode()


def _decrypt_file_info(body: bytes) -> dict[str, object]:
    import json as _json

    from cryptography.hazmat.primitives.ciphers import Cipher as _Cipher
    from cryptography.hazmat.primitives.ciphers import algorithms as _algorithms

    encrypted = _multipart_field(body, "fileInfo")
    cipher = _Cipher(_algorithms.AES(TOKEN[:16].encode()), modes.CBC(IV))
    decryptor = cipher.decryptor()
    padded = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
    decoded = _json.loads(padded[: -padded[-1]].decode())
    assert isinstance(decoded, dict)
    return decoded


def _upload_capture_handler(
    responses: dict[int, object] | None = None,
) -> tuple[object, list[dict[str, str]], list[bytes]]:
    requests: list[dict[str, str]] = []
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        body = request.content
        bodies.append(body)
        requests.append(
            {
                "partIndex": _multipart_field(body, "partIndex"),
                "uniqueId": _multipart_field(body, "uniqueId"),
                "fileSize": _multipart_field(body, "fileSize"),
                "totalPart": _multipart_field(body, "totalPart"),
            }
        )
        part_index = int(requests[-1]["partIndex"])
        response = (responses or {}).get(part_index)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}})
        return response

    return handler, requests, bodies


def _recovery_handler(
    listing: object,
    *,
    upload_responses: dict[int, object] | None = None,
    on_upload_request: Callable[[], None] | None = None,
) -> tuple[object, list[str]]:
    """Split wohome dispatcher calls by key: GetZoneInfo vs QueryAllFiles.

    ``requests`` records one tag per HTTP call: ``dispatcher:<key>`` for the
    encrypted dispatcher protocol and ``upload:<partIndex>`` for upload2C.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            payload = json.loads(request.content.decode())
            key = str(payload["header"]["key"])
            requests.append(f"dispatcher:{key}")
            if key == "QueryAllFiles":
                return _success_response(listing)
            return _success_response({"url": "https://upload.example.test"})
        part_index = int(_multipart_field(request.content, "partIndex"))
        requests.append(f"upload:{part_index}")
        if on_upload_request is not None:
            on_upload_request()
        response = (upload_responses or {}).get(part_index)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}})
        return response

    return handler, requests


# UAT listing fixture: entry-1 is the finished upload (name+size+fid match).
_RESUME_LISTING = {
    "systemDirs": [],
    "files": [
        {"id": "entry-1", "fid": "fid-recovered", "name": "report.bin", "type": "1", "size": 15},
        {"id": "entry-2", "fid": "fid-other", "name": "other.bin", "type": "1", "size": 15},
    ],
}


@pytest.mark.parametrize(
    ("file_size", "part_mb", "expected_part_size", "expected_total_parts"),
    [
        (0, 5, 5 * 1024 * 1024, 1),
        (5 * 1024 * 1024 + 1, 5, 5 * 1024 * 1024, 2),
        (1, 4, 5 * 1024 * 1024, 1),
        (1, 99, 16 * 1024 * 1024, 1),
    ],
)
def test_resolve_upload_part_plan_is_single_source(
    file_size: int, part_mb: int, expected_part_size: int, expected_total_parts: int
) -> None:
    part_size, total_parts = client_module.resolve_upload_part_plan(file_size, part_mb)

    assert part_size == expected_part_size
    assert total_parts == expected_total_parts


def test_upload_file_with_resume_sends_only_pending_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1/AC2：续传复用会话字段，仅发送未完成分片。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests, bodies = _upload_capture_handler()
    part_results: list[tuple[int, str]] = []
    resume = client_module.UploadResumeContext(
        unique_id="1690000000000",
        batch_no="20260101010101",
        completed_indexes=frozenset({1}),
        known_fid="",
        on_part_result=lambda index, fid: part_results.append((index, fid)),
    )

    item = _upload_client(handler).upload_file(
        "folder-1",
        local_file,
        upload_part_size_mb=5,
        max_upload_threads=2,
        resume=resume,
    )

    assert [request["partIndex"] for request in requests] == ["2", "3"]
    assert {request["uniqueId"] for request in requests} == {"1690000000000"}
    assert {request["fileSize"] for request in requests} == {"15"}
    assert {request["totalPart"] for request in requests} == {"3"}
    for body in bodies:
        file_info = _decrypt_file_info(body)
        assert file_info["batchNo"] == "20260101010101"
        assert file_info["fileSize"] == 15
    assert sorted(part_results) == [(2, "fid-1"), (3, "fid-1")]
    assert item.item_id == "fid-1"


def test_upload_file_prefers_first_response_with_fid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, _requests, _bodies = _upload_capture_handler(
        responses={
            2: httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-2"}}),
            3: httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-3"}}),
        }
    )
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1}),
    )

    # max_upload_threads=1 保证完成顺序等于提交顺序，first-with-fid 结果确定
    item = _upload_client(handler).upload_file(
        "folder-1", local_file, upload_part_size_mb=5, max_upload_threads=1, resume=resume
    )

    assert item.item_id == "fid-2"


def test_upload_file_with_resume_reports_seeded_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, _requests, _bodies = _upload_capture_handler()
    progress: list[tuple[int, int]] = []
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1}),
    )

    _upload_client(handler).upload_file(
        "folder-1",
        local_file,
        upload_part_size_mb=5,
        max_upload_threads=2,
        progress_callback=lambda done, total: progress.append((done, total)),
        resume=resume,
    )

    assert progress[-1] == (15, 15)
    assert sorted(progress) == [(10, 15), (15, 15)]


def test_upload_file_resume_filters_out_of_range_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests, _bodies = _upload_capture_handler()
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1, 7, 0, -2}),
    )

    _upload_client(handler).upload_file(
        "folder-1", local_file, upload_part_size_mb=5, max_upload_threads=2, resume=resume
    )

    assert sorted(request["partIndex"] for request in requests) == ["2", "3"]


def test_upload_file_all_parts_done_recovers_fid_from_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UAT 修正：全片完成但缺 fid 时不再重发末片，改为目录查询取回 fid。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests = _recovery_handler(_RESUME_LISTING)
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1, 2, 3}),
        known_fid="",
    )

    item = _upload_client(handler).upload_file(
        "folder-1", local_file, upload_part_size_mb=5, max_upload_threads=2, resume=resume
    )

    # 只有 QueryAllFiles dispatcher 请求：无 GetZoneInfo、无 upload2C 分片请求
    assert requests == ["dispatcher:QueryAllFiles"]
    assert item.item_id == "fid-recovered"
    assert item.download_id == "fid-recovered"
    assert item.name == "report.bin"
    assert item.size == 15
    assert item.parent_id == "folder-1"
    assert item.kind is WopanItemKind.FILE
    assert item.file_type == client_module.guess_upload_file_type("report.bin")


def test_upload_file_all_parts_done_listing_miss_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全片完成但目录查询未命中 → 抛出带清晰信息的 WopanResponseError。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests = _recovery_handler({"systemDirs": [], "files": []})
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1, 2, 3}),
        known_fid="",
    )

    with pytest.raises(WopanResponseError, match="未找到对应文件"):
        _upload_client(handler).upload_file(
            "folder-1", local_file, upload_part_size_mb=5, max_upload_threads=2, resume=resume
        )

    assert requests == ["dispatcher:QueryAllFiles"]


def test_upload_file_resume_with_known_fid_skips_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests, _bodies = _upload_capture_handler()
    progress: list[tuple[int, int]] = []
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1, 2, 3}),
        known_fid="known-fid",
    )

    item = _upload_client(handler).upload_file(
        "folder-1",
        local_file,
        upload_part_size_mb=5,
        progress_callback=lambda done, total: progress.append((done, total)),
        resume=resume,
    )

    # handler 只在 upload2C 请求时记录，requests 为空即零上传网络请求
    assert requests == []
    assert item.item_id == "known-fid"
    assert item.file_type == client_module.guess_upload_file_type("report.bin")
    assert progress == [(15, 15)]


def test_upload_file_single_part_resume_recovers_fid_from_listing(tmp_path: Path) -> None:
    """单片路径同样接入目录查询自愈（替换原"重发末片"兜底）。"""
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")
    handler, requests = _recovery_handler(
        {
            "systemDirs": [],
            "files": [
                {
                    "id": "entry-1",
                    "fid": "fid-single",
                    "name": "report.txt",
                    "type": "1",
                    "size": 7,
                },
            ],
        }
    )
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1}),
        known_fid="",
    )

    item = _upload_client(handler).upload_file("0", local_file, resume=resume)

    assert requests == ["dispatcher:QueryAllFiles"]
    assert item.item_id == "fid-single"


def test_upload_file_single_part_resume_with_fid_skips_network(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")
    handler, requests, _bodies = _upload_capture_handler()
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1}),
        known_fid="single-fid",
    )

    item = _upload_client(handler).upload_file("0", local_file, resume=resume)

    assert requests == []
    assert item.item_id == "single-fid"
    assert item.size == 7


def test_upload_file_all_parts_done_recovery_reports_final_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录查询自愈成功后回调满额进度（替换原"重发末片"进度兜底）。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests = _recovery_handler(_RESUME_LISTING)
    progress: list[tuple[int, int]] = []
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1, 2, 3}),
        known_fid="",
    )

    _upload_client(handler).upload_file(
        "folder-1",
        local_file,
        upload_part_size_mb=5,
        progress_callback=lambda done, total: progress.append((done, total)),
        resume=resume,
    )

    assert requests == ["dispatcher:QueryAllFiles"]
    assert progress == [(15, 15)]


@pytest.mark.parametrize("with_progress", [True, False])
def test_upload_file_resume_part_failure_recovers_from_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_progress: bool
) -> None:
    """失败路径自愈 a)：续传分片失败后目录查询命中 → 整体按成功返回。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests = _recovery_handler(
        _RESUME_LISTING,
        upload_responses={2: httpx.Response(200, json={"code": "9999", "msg": "busy"})},
    )
    progress: list[tuple[int, int]] = []
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1}),
    )

    item = _upload_client(handler).upload_file(
        "folder-1",
        local_file,
        upload_part_size_mb=5,
        max_upload_threads=1,
        retry_max_attempts=0,
        progress_callback=(
            (lambda done, total: progress.append((done, total))) if with_progress else None
        ),
        resume=resume,
    )

    assert item.item_id == "fid-recovered"
    if with_progress:
        assert progress == [(15, 15)]
    else:
        assert progress == []
    assert "upload:2" in requests
    assert requests.count("dispatcher:QueryAllFiles") == 1


@pytest.mark.parametrize(
    "listing",
    [
        {"systemDirs": [], "files": []},
        {
            "systemDirs": [],
            "files": [
                {"id": "e1", "fid": "fid-old", "name": "report.bin", "type": "1", "size": 14},
            ],
        },
        {
            "systemDirs": [],
            "files": [
                {"id": "e1", "name": "report.bin", "type": "1", "size": 15},
            ],
        },
        {
            "systemDirs": [],
            "files": [
                {"id": "d1", "name": "report.bin", "type": "0"},
            ],
        },
    ],
    ids=["empty", "size-mismatch", "no-fid", "same-name-folder"],
)
def test_upload_file_resume_failure_keeps_original_error_when_listing_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: object
) -> None:
    """失败路径自愈 b)/e)：未命中（含同名不同 size / 无 fid / 同名目录）→ 原异常保留。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests = _recovery_handler(
        listing,
        upload_responses={2: httpx.Response(200, json={"code": "9999", "msg": "busy"})},
    )
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1}),
    )

    with pytest.raises(WopanBusinessError, match="busy"):
        _upload_client(handler).upload_file(
            "folder-1",
            local_file,
            upload_part_size_mb=5,
            max_upload_threads=1,
            retry_max_attempts=0,
            resume=resume,
        )

    assert requests.count("dispatcher:QueryAllFiles") == 1


def test_upload_file_resume_failure_keeps_original_error_when_listing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败路径自愈：目录查询自身失败（响应不可解析）不得掩盖原上传异常。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, requests = _recovery_handler(
        {"files": "oops"},  # QueryAllFiles field-not-list → list_files 抛 WopanResponseError
        upload_responses={2: httpx.Response(200, json={"code": "9999", "msg": "busy"})},
    )
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset({1}),
    )

    with pytest.raises(WopanBusinessError, match="busy"):
        _upload_client(handler).upload_file(
            "folder-1",
            local_file,
            upload_part_size_mb=5,
            max_upload_threads=1,
            retry_max_attempts=0,
            resume=resume,
        )

    assert requests.count("dispatcher:QueryAllFiles") == 1


def test_upload_file_cancelled_error_propagates_without_listing_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败路径自愈 c)：WopanUploadCancelledError 不触发目录查询、原样传播。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")
    state = {"cancelled": False}

    def cancel_requested() -> bool:
        return state["cancelled"]

    def mark_cancelled() -> None:
        state["cancelled"] = True

    handler, requests = _recovery_handler(_RESUME_LISTING, on_upload_request=mark_cancelled)
    resume = client_module.UploadResumeContext(unique_id="u", batch_no="b")

    with pytest.raises(WopanUploadCancelledError):
        _upload_client(handler).upload_file(
            "0", local_file, resume=resume, cancel_requested=cancel_requested
        )

    # 取消即传播：即使目录里有完全匹配的文件也不做查询
    assert "dispatcher:QueryAllFiles" not in requests


def test_upload_file_fresh_failure_makes_no_listing_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败路径自愈 d)：resume=None 的全新上传失败零额外目录查询请求。"""
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "tiny.bin"
    local_file.write_bytes(b"ab")  # 单片文件
    handler, requests = _recovery_handler(
        _RESUME_LISTING,
        upload_responses={1: httpx.Response(200, json={"code": "9999", "msg": "busy"})},
    )

    with pytest.raises(WopanBusinessError, match="busy"):
        _upload_client(handler).upload_file("folder-1", local_file, retry_max_attempts=0)

    assert requests == ["dispatcher:GetZoneInfo", "upload:1"]


def test_upload_parts_parallel_reraises_unexpected_part_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"012345678901234")
    handler, _requests, _bodies = _upload_capture_handler(
        responses={2: RuntimeError("boom")},
    )

    with pytest.raises(RuntimeError, match="boom"):
        _upload_client(handler).upload_file(
            "folder-1",
            local_file,
            upload_part_size_mb=5,
            max_upload_threads=2,
            retry_max_attempts=0,
        )


def test_upload_parts_parallel_defends_against_empty_pending(
    tmp_path: Path,
) -> None:
    wopan = _upload_client(_upload_handler(httpx.Response(200, json={})))
    local_file = tmp_path / "f.bin"
    local_file.write_bytes(b"x")

    with pytest.raises(WopanResponseError, match="no response"):
        wopan._upload_parts_parallel(
            "https://upload.example.test/openapi/client/upload2C",
            {},
            "f.bin",
            "application/octet-stream",
            local_file,
            part_size=1,
            total_parts=1,
            max_workers=1,
            max_attempts=1,
            completed_indexes={1},
        )


def test_upload_part_reports_empty_fid_without_data_object(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"content")
    handler, _requests, _bodies = _upload_capture_handler(
        responses={1: httpx.Response(200, json={"code": "0000"})},
    )
    part_results: list[tuple[int, str]] = []
    resume = client_module.UploadResumeContext(
        unique_id="u",
        batch_no="b",
        completed_indexes=frozenset(),
        on_part_result=lambda index, fid: part_results.append((index, fid)),
    )

    with pytest.raises(WopanResponseError, match="data is not an object"):
        _upload_client(handler).upload_file("0", local_file, resume=resume)

    assert part_results == [(1, "")]
