from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan.client import WopanClient
from openwopan.wopan.errors import WopanBusinessError, WopanResponseError
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


def _encrypt_wohome_payload(payload: object) -> str:
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


def _multipart_parts(request: httpx.Request) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    content_type = request.headers["Content-Type"]
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + request.content
    )
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        payload = part.get_payload(decode=True) or b""
        file_name = part.get_filename()
        if file_name:
            files[name] = (file_name, payload)
        else:
            fields[name] = payload.decode()
    return fields, files


def _success_response(data: object) -> httpx.Response:
    response_data = data if isinstance(data, str) else _encrypt_wohome_payload(data)
    return _json_response(
        {
            "STATUS": "200",
            "MSG": "ok",
            "RSP": {
                "RSP_CODE": "0000",
                "RSP_DESC": "success",
                "DATA": response_data,
            },
        }
    )


def _client_and_captured_params(
    responses: list[httpx.Response],
) -> tuple[WopanClient, list[tuple[str, dict[str, object]]]]:
    captured: list[tuple[str, dict[str, object]]] = []
    response_iter: Iterator[httpx.Response] = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["header"]["channel"] == "wohome"
        assert request.headers["Accesstoken"] == TOKEN
        key = str(body["header"]["key"])
        param = _decrypt_wohome_payload(body["body"]["param"])
        captured.append((key, param))
        return next(response_iter)

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, captured


def test_create_folder_calls_create_directory_and_returns_folder_item() -> None:
    client, captured = _client_and_captured_params([_success_response({"id": "folder-1"})])

    item = client.create_folder("0", "Reports")

    assert item.item_id == "folder-1"
    assert item.name == "Reports"
    assert item.kind is WopanItemKind.FOLDER
    assert item.parent_id == "0"
    assert captured == [
        (
            "CreateDirectory",
            {
                "spaceType": "0",
                "familyId": "0",
                "parentDirectoryId": "0",
                "directoryName": "Reports",
                "clientId": "1001000021",
            },
        )
    ]


def test_query_cloud_usage_calls_usage_api_and_returns_usage_model() -> None:
    client, captured = _client_and_captured_params(
        [
            _success_response(
                {
                    "usageInfo": {
                        "byteUsedSize": 1024,
                        "byteTotalSize": "2048",
                    },
                    "vipLevel": "3",
                    "expireTime": "20270619235959",
                }
            )
        ]
    )

    usage = client.query_cloud_usage("13800138000")

    assert usage.used_bytes == 1024
    assert usage.total_bytes == 2048
    assert usage.vip_level == "3"
    assert usage.expire_time == "20270619235959"
    assert captured == [
        (
            "QueryCloudUsageInfo",
            {
                "phoneNum": "13800138000",
                "clientId": "1001000021",
            },
        )
    ]


def test_query_cloud_usage_rejects_missing_usage_info() -> None:
    client, _captured = _client_and_captured_params([_success_response({})])

    with pytest.raises(WopanResponseError, match="usageInfo"):
        client.query_cloud_usage("13800138000")


def test_create_folder_requires_created_id() -> None:
    client, _captured = _client_and_captured_params([_success_response({})])

    with pytest.raises(WopanResponseError, match="missing id"):
        client.create_folder("0", "Reports")


def test_rename_file_calls_rename_file_or_directory_with_kind_and_file_type() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.rename("file-1", "renamed.txt", WopanItemKind.FILE, "4")

    assert captured == [
        (
            "RenameFileOrDirectory",
            {
                "spaceType": "0",
                "type": 1,
                "fileType": "4",
                "id": "file-1",
                "name": "renamed.txt",
                "clientId": "1001000021",
            },
        )
    ]


def test_delete_folder_routes_id_to_dir_list() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.delete("folder-1", WopanItemKind.FOLDER)

    assert captured == [
        (
            "DeleteFile",
            {
                "spaceType": "0",
                "vipLevel": "0",
                "dirList": ["folder-1"],
                "fileList": [],
                "clientId": "1001000021",
            },
        )
    ]


def test_move_file_routes_id_to_file_list() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.move("file-1", WopanItemKind.FILE, "folder-2")

    assert captured == [
        (
            "MoveFile",
            {
                "targetDirId": "folder-2",
                "sourceType": "0",
                "targetType": "0",
                "dirList": [],
                "fileList": ["file-1"],
                "secret": False,
                "clientId": "1001000021",
            },
        )
    ]


def test_upload_file_gets_zone_and_posts_single_part(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"upload-content")
    captured_dispatch: list[tuple[str, dict[str, object], bool]] = []
    upload_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            body = json.loads(request.content)
            key = str(body["header"]["key"])
            param = _decrypt_wohome_payload(body["body"]["param"])
            captured_dispatch.append((key, param, body["body"].get("key") is True))
            return _success_response({"url": "https://upload.example.test"})
        upload_requests.append(request)
        return httpx.Response(
            200,
            json={"code": "0000", "data": {"fid": "fid-1"}, "msg": "ok"},
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    item = client.upload_file("folder-1", local_file)

    assert item.item_id == "fid-1"
    assert item.download_id == "fid-1"
    assert item.name == "report.txt"
    assert item.kind is WopanItemKind.FILE
    assert item.parent_id == "folder-1"
    assert item.file_type == "4"
    assert item.size == len(b"upload-content")
    assert captured_dispatch == [
        ("GetZoneInfo", {"appId": "10000001"}, True),
    ]
    assert len(upload_requests) == 1
    request = upload_requests[0]
    assert str(request.url) == "https://upload.example.test/openapi/client/upload2C"
    assert request.headers["Origin"] == "https://pan.wo.cn"
    assert request.headers["Referer"] == "https://pan.wo.cn/"
    assert "Mozilla/5.0" in request.headers["User-Agent"]

    fields, files = _multipart_parts(request)
    assert fields["accessToken"] == TOKEN
    assert fields["fileName"] == "report.txt"
    assert fields["psToken"] == "undefined"
    assert fields["fileSize"] == str(len(b"upload-content"))
    assert fields["totalPart"] == "1"
    assert fields["partSize"] == str(len(b"upload-content"))
    assert fields["partIndex"] == "1"
    assert fields["channel"] == "wocloud"
    assert fields["directoryId"] == "folder-1"
    assert fields["uniqueId"].isdigit()
    assert files["file"] == ("report.txt", b"upload-content")
    file_info = _decrypt_wohome_payload(fields["fileInfo"])
    assert file_info == {
        "spaceType": "0",
        "directoryId": "folder-1",
        "batchNo": file_info["batchNo"],
        "fileName": "report.txt",
        "fileSize": len(b"upload-content"),
        "fileType": "4",
    }
    assert isinstance(file_info["batchNo"], str)
    assert len(file_info["batchNo"]) == 14


def test_upload_file_posts_multiple_parts_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"abcdefghijklmnopq")
    upload_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        upload_requests.append(request)
        return httpx.Response(
            200,
            json={"code": "0000", "data": {"fid": "fid-1"}, "msg": "ok"},
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    item = client.upload_file(
        "folder-1",
        local_file,
        upload_part_size_mb=5,
        max_upload_threads=2,
    )

    assert item.download_id == "fid-1"
    assert len(upload_requests) == 4
    parts: dict[int, bytes] = {}
    for request in upload_requests:
        fields, files = _multipart_parts(request)
        assert fields["totalPart"] == "4"
        part_index = int(fields["partIndex"])
        content = files["file"][1]
        assert fields["partSize"] == str(len(content))
        parts[part_index] = content
    assert parts == {
        1: b"abcde",
        2: b"fghij",
        3: b"klmno",
        4: b"pq",
    }


def test_upload_file_falls_back_to_default_zone_url(tmp_path: Path) -> None:
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"x")
    upload_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({})
        upload_urls.append(str(request.url))
        return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}})

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.upload_file("0", local_file)

    assert upload_urls == ["https://tjupload.pan.wo.cn/openapi/client/upload2C"]


def test_upload_file_maps_non_success_upload_response(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_text("content")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        return httpx.Response(200, json={"code": "9999", "msg": "failed"})

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanBusinessError, match="failed"):
        client.upload_file("0", local_file)


def test_get_download_info_calls_get_download_url_and_returns_url() -> None:
    client, captured = _client_and_captured_params(
        [
            _success_response(
                [
                    {
                        "fid": "file-1",
                        "downloadUrl": "https://download.example.test/file-1",
                    }
                ]
            )
        ]
    )

    info = client.get_download_info("file-1")

    assert info.url == "https://download.example.test/file-1"
    assert captured == [
        (
            "GetDownloadUrl",
            {
                "fidList": ["file-1"],
                "clientId": "1001000021",
                "spaceType": "0",
            },
        )
    ]


def test_get_download_info_selects_requested_file_from_response_list() -> None:
    client, _captured = _client_and_captured_params(
        [
            _success_response(
                [
                    {
                        "fid": "other-file",
                        "downloadUrl": "https://download.example.test/other",
                    },
                    {
                        "fid": "file-1",
                        "downloadUrl": "https://download.example.test/file-1",
                    },
                ]
            )
        ]
    )

    info = client.get_download_info("file-1")

    assert info.url == "https://download.example.test/file-1"


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({}, "not a list"),
        ([], "empty"),
        ([{"fid": "file-1"}], "missing downloadUrl"),
        ([{"fid": "other", "downloadUrl": "https://download.example.test/other"}], "missing"),
    ],
)
def test_get_download_info_rejects_malformed_response(data: object, match: str) -> None:
    client, _captured = _client_and_captured_params([_success_response(data)])

    with pytest.raises(WopanResponseError, match=match):
        client.get_download_info("file-1")


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        (lambda client: client.create_folder("", "name"), "parent_id"),
        (lambda client: client.create_folder("0", ""), "name"),
        (lambda client: client.rename("", "name", WopanItemKind.FOLDER), "item_id"),
        (lambda client: client.rename("item-1", "", WopanItemKind.FOLDER), "new_name"),
        (lambda client: client.delete("", WopanItemKind.FILE), "item_id"),
        (lambda client: client.move("", WopanItemKind.FILE, "0"), "item_id"),
        (lambda client: client.move("item-1", WopanItemKind.FILE, ""), "target_parent_id"),
        (lambda client: client.upload_file("", Path("report.txt")), "parent_id"),
        (lambda client: client.get_download_info(""), "download_id"),
    ],
)
def test_file_operations_validate_required_fields(
    operation: object,
    match: str,
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = WopanClient(COOKIE_HEADER, http_client=httpx.Client(transport=transport))

    with pytest.raises(ValueError, match=match):
        operation(client)  # type: ignore[operator]
