from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan import client as client_module
from openwopan.wopan.client import WopanClient
from openwopan.wopan.errors import (
    WopanBusinessError,
    WopanResponseError,
    WopanUploadCancelledError,
)
from openwopan.wopan.models import WopanItemKind
from openwopan.wopan.naming import server_file_name

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
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.content
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


def _rsp_code_response(code: str, data: object) -> httpx.Response:
    return _json_response(
        {
            "STATUS": "200",
            "MSG": "ok",
            "RSP": {
                "RSP_CODE": code,
                "RSP_DESC": "desc",
                "DATA": _encrypt_wohome_payload(data),
            },
        }
    )


def test_create_folder_reuse_existing_sends_is_could_repeat_and_reuses_130007_id() -> None:
    """reuse_existing：请求带 isCouldRepeat="1"，130007（同名目录已存在）
    按成功处理并复用 DATA.id —— 官方客户端文件夹上传的目录合并契约。"""
    client, captured = _client_and_captured_params(
        [_rsp_code_response("130007", {"id": "existing-dir"})]
    )

    item = client.create_folder("0", "Reports", reuse_existing=True)

    assert item.item_id == "existing-dir"
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
                "isCouldRepeat": "1",
            },
        )
    ]


def test_create_folder_reuse_existing_accepts_new_directory_too() -> None:
    """reuse_existing 下全新目录照常创建（0000 + 新 id）。"""
    client, captured = _client_and_captured_params([_success_response({"id": "dir-9"})])

    item = client.create_folder("0", "Reports", reuse_existing=True)

    assert item.item_id == "dir-9"
    assert captured[0][1]["isCouldRepeat"] == "1"


def test_create_folder_without_reuse_still_rejects_130007() -> None:
    """默认路径保持原语义：不带 isCouldRepeat，130007 仍是业务错误。"""
    client, captured = _client_and_captured_params(
        [_rsp_code_response("130007", {"id": "existing-dir"})]
    )

    with pytest.raises(WopanBusinessError) as excinfo:
        client.create_folder("0", "Reports")

    assert excinfo.value.code == "130007"
    assert "isCouldRepeat" not in captured[0][1]


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


def test_delete_many_sends_one_request_with_dir_and_file_lists() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.delete_many(
        [
            ("folder-1", WopanItemKind.FOLDER),
            ("file-1", WopanItemKind.FILE),
            ("folder-2", WopanItemKind.FOLDER),
        ]
    )

    assert captured == [
        (
            "DeleteFile",
            {
                "spaceType": "0",
                "vipLevel": "0",
                "dirList": ["folder-1", "folder-2"],
                "fileList": ["file-1"],
                "clientId": "1001000021",
            },
        )
    ]


def test_move_many_sends_one_request_with_dir_and_file_lists() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.move_many(
        [("file-1", WopanItemKind.FILE), ("folder-1", WopanItemKind.FOLDER)],
        "folder-2",
    )

    assert captured == [
        (
            "MoveFile",
            {
                "targetDirId": "folder-2",
                "sourceType": "0",
                "targetType": "0",
                "dirList": ["folder-1"],
                "fileList": ["file-1"],
                "secret": False,
                "clientId": "1001000021",
            },
        )
    ]


def test_copy_many_sends_one_request_with_dir_and_file_lists() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.copy_many(
        [("file-1", WopanItemKind.FILE), ("folder-1", WopanItemKind.FOLDER)],
        "folder-2",
    )

    assert captured == [
        (
            "CopyFile",
            {
                "targetDirId": "folder-2",
                "sourceType": "0",
                "targetType": "0",
                "dirList": ["folder-1"],
                "fileList": ["file-1"],
                "secret": False,
                "clientId": "1001000021",
            },
        )
    ]


def test_search_files_sends_request_and_maps_items() -> None:
    client, captured = _client_and_captured_params(
        [
            _success_response(
                {
                    "personalResult": [
                        {
                            "id": "file-1",
                            "fileName": "report.txt",
                            "fileSize": "2048",
                            "type": "",
                            "fid": "fid-1",
                            "fileType": "5",
                            "directoryId": "0",
                        },
                        {
                            "id": "folder-1",
                            "fileName": "docs",
                            "fileSize": 0,
                            "type": "0",
                        },
                    ],
                    "familyResult": [],
                }
            )
        ]
    )

    items = client.search_files("report", page_no=2, page_size=10)

    assert captured == [
        (
            "SearchFile",
            {
                "searchType": "2",
                "keyWord": "report",
                "pageNo": 2,
                "pageSize": 10,
                "clientId": "1001000021",
            },
        )
    ]
    assert [item.name for item in items] == ["report.txt", "docs"]
    assert [item.kind for item in items] == [
        WopanItemKind.FILE,
        WopanItemKind.FOLDER,
    ]
    assert items[0].size == 2048
    assert items[0].download_id == "fid-1"
    assert items[0].parent_id == "0"
    assert items[0].file_type == "5"
    assert items[0].updated_at is None


def test_search_files_returns_empty_list_without_personal_result() -> None:
    client, captured = _client_and_captured_params([_success_response({})])

    items = client.search_files("nothing")

    assert items == []
    assert captured[0][0] == "SearchFile"


def test_search_files_rejects_non_list_personal_result() -> None:
    client, _captured = _client_and_captured_params([_success_response({"personalResult": "nope"})])

    with pytest.raises(WopanResponseError, match="personalResult is not a list"):
        client.search_files("kw")


@pytest.mark.parametrize(
    ("raw_item", "match"),
    [
        ({"fileName": "a.txt", "type": "1"}, "missing id"),
        ({"id": "file-1", "type": "1"}, "missing fileName"),
    ],
)
def test_search_files_rejects_malformed_items(raw_item: dict[str, object], match: str) -> None:
    client, _captured = _client_and_captured_params(
        [_success_response({"personalResult": [raw_item]})]
    )

    with pytest.raises(WopanResponseError, match=match):
        client.search_files("kw")


def test_search_files_rejects_non_dict_item() -> None:
    client, _captured = _client_and_captured_params(
        [_success_response({"personalResult": ["str-item"]})]
    )

    with pytest.raises(WopanResponseError, match="item is not an object"):
        client.search_files("kw")


def test_search_files_falls_back_to_size_field() -> None:
    client, _captured = _client_and_captured_params(
        [
            _success_response(
                {
                    "personalResult": [
                        {"id": "file-1", "fileName": "a.txt", "type": "1", "size": 4096}
                    ]
                }
            )
        ]
    )

    items = client.search_files("a")

    assert items[0].size == 4096


def test_get_directory_path_sends_request_and_orders_root_first() -> None:
    client, captured = _client_and_captured_params(
        [
            _success_response(
                [
                    {"id": "folder-2", "directoryName": "2"},
                    {"id": "folder-1", "directoryName": "test"},
                    {"id": "0", "directoryName": "个人云"},
                ]
            )
        ]
    )

    chain = client.get_directory_path("folder-2")

    assert captured == [
        (
            "GetDirectoryPath",
            {"directoryId": "folder-2", "clientId": "1001000021"},
        )
    ]
    assert chain == [
        ("0", "个人云"),
        ("folder-1", "test"),
        ("folder-2", "2"),
    ]


def test_get_directory_path_rejects_malformed_responses() -> None:
    client, _captured = _client_and_captured_params([_success_response({"id": "x"})])

    with pytest.raises(WopanResponseError, match="DATA is not a list"):
        client.get_directory_path("folder-2")


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (["str-item"], "item is not an object"),
        ([{"id": "folder-1"}], "missing id or name"),
        ([{"directoryName": "test"}], "missing id or name"),
    ],
)
def test_get_directory_path_rejects_malformed_items(data: list[object], match: str) -> None:
    client, _captured = _client_and_captured_params([_success_response(data)])

    with pytest.raises(WopanResponseError, match=match):
        client.get_directory_path("folder-2")


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


def test_upload_file_retries_transient_upload_zone_gateway_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(client_module, "_sleep_cancelable", lambda s, _c: sleeps.append(s))
    local_file = tmp_path / "report.txt"
    local_file.write_text("upload-content")
    zone_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal zone_attempts
        if str(request.url).endswith("/wohome/dispatcher"):
            zone_attempts += 1
            if zone_attempts == 1:
                return httpx.Response(504, request=request)
            return _success_response({"url": "https://upload.example.test"})
        return httpx.Response(
            200,
            json={"code": "0000", "data": {"fid": "fid-1"}, "msg": "ok"},
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    item = client.upload_file("folder-1", local_file, retry_max_attempts=1)

    assert item.item_id == "fid-1"
    assert zone_attempts == 2
    assert sleeps == [5.0]  # 官方契约：重试间隔 5 秒


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


def test_upload_cancel_before_network_starts(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"data")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _success_response({"url": "https://upload.example.test"})

    client = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(WopanUploadCancelledError):
        client.upload_file("0", local_file, cancel_requested=lambda: True)

    assert requests == []


def test_upload_cancel_after_zone_request_does_not_start_part(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"data")
    requests: list[httpx.Request] = []
    cancelled = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        requests.append(request)
        cancelled = True
        return _success_response({"url": "https://upload.example.test"})

    client = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(WopanUploadCancelledError):
        client.upload_file("0", local_file, cancel_requested=lambda: cancelled)

    assert len(requests) == 1


def test_upload_cancel_after_inflight_part_does_not_report_success(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"data")
    cancelled = False
    requests: list[httpx.Request] = []
    progress: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        requests.append(request)
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        cancelled = True
        return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}})

    client = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(WopanUploadCancelledError):
        client.upload_file(
            "0",
            local_file,
            cancel_requested=lambda: cancelled,
            progress_callback=lambda done, total: progress.append((done, total)),
        )

    assert len(requests) == 2
    assert progress == []


def test_upload_cancel_queued_parts_before_they_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openwopan.wopan.client.BYTES_PER_MB", 1)
    local_file = tmp_path / "report.bin"
    local_file.write_bytes(b"abcdefghijklmnopq")
    cancelled = False
    part_requests: list[httpx.Request] = []
    progress: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        part_requests.append(request)
        cancelled = True
        return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}})

    client = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(WopanUploadCancelledError):
        client.upload_file(
            "0",
            local_file,
            upload_part_size_mb=5,
            max_upload_threads=1,
            cancel_requested=lambda: cancelled,
            progress_callback=lambda done, total: progress.append((done, total)),
        )

    assert len(part_requests) == 1
    assert progress == []


def test_upload_cancel_prevents_part_retry_after_http_error(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"data")
    cancelled = False
    part_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled, part_attempts
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        part_attempts += 1
        cancelled = True
        return httpx.Response(503, request=request)

    client = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(WopanUploadCancelledError):
        client.upload_file("0", local_file, cancel_requested=lambda: cancelled)

    assert part_attempts == 1


def test_upload_cancel_prevents_zone_retry(tmp_path: Path) -> None:
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"data")
    cancelled = False
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        requests.append(request)
        cancelled = True
        return httpx.Response(504, request=request)

    client = WopanClient(
        COOKIE_HEADER, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(WopanUploadCancelledError):
        client.upload_file("0", local_file, cancel_requested=lambda: cancelled)

    assert len(requests) == 1


def test_sleep_cancelable_returns_after_duration() -> None:
    """重试等待正常走完（短时长验证，不拖慢测试）。"""
    import time as time_module

    started = time_module.monotonic()
    client_module._sleep_cancelable(0.3, None)
    assert time_module.monotonic() - started >= 0.3


def test_sleep_cancelable_raises_promptly_on_cancel() -> None:
    """重试等待期间取消：0.2s 步进内立即抛出，上传取消保持灵敏。"""
    import time as time_module

    started = time_module.monotonic()
    with pytest.raises(WopanUploadCancelledError):
        client_module._sleep_cancelable(5.0, lambda: True)
    assert time_module.monotonic() - started < 1.0


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


def test_upload_file_maps_non_success_upload_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_sleep_cancelable", lambda *_args: None)
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


def test_upload_part_failure_logs_server_error_body(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分片 500：每次重试的日志必须带上服务端响应体片段——裸 500 的唯一
    文字线索（此前只记状态码，服务端给的原因被 raise_for_status 丢弃）。"""
    monkeypatch.setattr(client_module, "_sleep_cancelable", lambda *_args: None)
    local_file = tmp_path / "report.txt"
    local_file.write_text("content")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/wohome/dispatcher"):
            return _success_response({"url": "https://upload.example.test"})
        return httpx.Response(500, json={"error": "part assembly failed"})

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level("WARNING", logger="openwopan.wopan.client"):
        with pytest.raises(httpx.HTTPStatusError):
            client.upload_file("0", local_file)

    attempt_lines = [
        line for line in caplog.text.splitlines() if "upload_part.attempt_failed" in line
    ]
    assert len(attempt_lines) == 4  # 默认重试 3 次 → 共 4 次尝试，每次都留痕
    assert "status=500" in caplog.text
    assert "part assembly failed" in caplog.text  # 响应体片段入库
    assert "upload_file.http_error" in caplog.text


# ---------------------------------------------------------------------------
# Quick transfer (秒传) — instant upload by content hash, official-client
# parity: full-file SHA-256 → POST b.smartont.net quickTransfer; hasFile=1
# completes without any part; anything else falls back to the chunked upload.
# ---------------------------------------------------------------------------

QUICK_TRANSFER_CONTENT = b"q" * (3 * 1024 * 1024)  # 恰好到达官方 3MB 探测阈值


def _upload_flow_client(
    quick_responses: list[httpx.Response],
    dispatch_responses: list[httpx.Response],
) -> tuple[WopanClient, list[dict[str, object]], list[httpx.Request]]:
    """Client double routing smartont（秒传）/wohome dispatch/分片上传三类流量。

    ``quick_responses`` 按探测尝试顺序逐个返回（官方契约：最多 3 次尝试）。
    """
    quick_payloads: list[dict[str, object]] = []
    upload_requests: list[httpx.Request] = []
    dispatch_iter: Iterator[httpx.Response] = iter(dispatch_responses)
    quick_iter: Iterator[httpx.Response] = iter(quick_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "b.smartont.net":
            assert request.headers["access-token"] == TOKEN
            quick_payloads.append(json.loads(request.content))
            return next(quick_iter)
        if str(request.url).endswith("/wohome/dispatcher"):
            return next(dispatch_iter)
        upload_requests.append(request)
        return httpx.Response(
            200, json={"code": "0000", "data": {"fid": "fid-uploaded"}, "msg": "ok"}
        )

    client = WopanClient(
        COOKIE_HEADER,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, quick_payloads, upload_requests


def _quick_hit_response(has_file: int, fid: str = "") -> httpx.Response:
    result: dict[str, object] = {"hasFile": has_file}
    if fid:
        result["fid"] = fid
    return httpx.Response(200, json={"meta": {"code": "0000"}, "result": result})


def test_upload_file_quick_transfer_hit_completes_without_parts(tmp_path: Path) -> None:
    """秒传命中：零分片、零 zone 请求，一次探测即完成并上报满进度。"""
    local_file = tmp_path / "movie.mkv"
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [_quick_hit_response(1, fid="fid-qt")], []
    )
    progress: list[tuple[int, int]] = []

    item = client.upload_file(
        "folder-1",
        local_file,
        quick_transfer=True,
        progress_callback=lambda uploaded, total: progress.append((uploaded, total)),
    )

    assert item.item_id == "fid-qt"
    assert item.download_id == "fid-qt"
    assert item.name == "movie.mkv"
    assert item.size == len(QUICK_TRANSFER_CONTENT)
    assert item.parent_id == "folder-1"
    assert upload_requests == []  # 没有任何分片上传
    assert progress == [(len(QUICK_TRANSFER_CONTENT), len(QUICK_TRANSFER_CONTENT))]
    (payload,) = quick_payloads
    assert payload["sha256"] == hashlib.sha256(QUICK_TRANSFER_CONTENT).hexdigest()
    assert payload["fileName"] == "movie.mkv"
    assert payload["fileType"] == "2"  # mkv → 视频类型码
    assert payload["fileSize"] == len(QUICK_TRANSFER_CONTENT)
    assert payload["directoryId"] == "folder-1"
    assert payload["spaceType"] == "0"
    assert isinstance(payload["fileModificationTime"], int)
    batch_no = str(payload["batchNo"])
    assert len(batch_no) == 14 and batch_no.isdigit()


def test_upload_file_quick_transfer_miss_falls_back_to_chunk_upload(tmp_path: Path) -> None:
    """秒传未命中（hasFile=0）：回落分片上传，流程照常完成。"""
    local_file = tmp_path / "movie.mkv"
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [_quick_hit_response(0)], [_success_response({"url": "https://upload.example.test"})]
    )

    item = client.upload_file("folder-1", local_file, quick_transfer=True)

    assert item.item_id == "fid-uploaded"
    assert len(upload_requests) == 1
    assert str(upload_requests[0].url) == "https://upload.example.test/openapi/client/upload2C"
    assert len(quick_payloads) == 1


def test_upload_file_quick_transfer_probe_error_falls_back(tmp_path: Path) -> None:
    """探测三次尝试全 500：秒传失败绝不阻塞上传，回落分片照常完成。"""
    local_file = tmp_path / "movie.mkv"
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [httpx.Response(500, json={})] * 3,
        [_success_response({"url": "https://upload.example.test"})],
    )

    item = client.upload_file("folder-1", local_file, quick_transfer=True)

    assert item.item_id == "fid-uploaded"
    assert len(upload_requests) == 1
    assert len(quick_payloads) == 3  # 官方契约：重试 2 次，共 3 次尝试


def test_upload_file_skips_quick_transfer_by_default(tmp_path: Path) -> None:
    """默认关闭：协议层不主动探测（由服务层按设置开启）。"""
    local_file = tmp_path / "movie.mkv"
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [_quick_hit_response(1, fid="fid-qt")],
        [_success_response({"url": "https://upload.example.test"})],
    )

    item = client.upload_file("folder-1", local_file)

    assert item.item_id == "fid-uploaded"  # 走了正常上传
    assert quick_payloads == []


def test_upload_file_quick_transfer_skips_small_files(tmp_path: Path) -> None:
    """小于 3MB 阈值：即使开启也不探测（官方客户端同款门限）。"""
    local_file = tmp_path / "tiny.txt"
    local_file.write_bytes(b"tiny")
    client, quick_payloads, upload_requests = _upload_flow_client(
        [_quick_hit_response(1, fid="fid-qt")],
        [_success_response({"url": "https://upload.example.test"})],
    )

    item = client.upload_file("folder-1", local_file, quick_transfer=True)

    assert item.item_id == "fid-uploaded"
    assert quick_payloads == []
    assert len(upload_requests) == 1


def test_upload_file_quick_transfer_hit_without_fid_recovers_from_listing(
    tmp_path: Path,
) -> None:
    """命中但响应无 fid：按列表恢复（含长名截断存储形态），绝不重发分片。"""
    long_name = "b" * 99 + ".mkv"  # 103 字符，服务端存 100 字符截断名
    stored_name = server_file_name(long_name)
    local_file = tmp_path / long_name
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [_quick_hit_response(1)],
        [
            _success_response(
                {
                    "files": [
                        {
                            "id": "item-1",
                            "name": stored_name,
                            "type": "1",
                            "fid": "fid-listed",
                            "size": len(QUICK_TRANSFER_CONTENT),
                        }
                    ]
                }
            )
        ],
    )

    item = client.upload_file("folder-1", local_file, upload_name=long_name, quick_transfer=True)

    assert item.item_id == "fid-listed"
    assert item.download_id == "fid-listed"
    assert item.name == long_name
    assert upload_requests == []  # 未重发任何分片


def test_upload_file_quick_transfer_hit_without_fid_and_missing_listing_raises(
    tmp_path: Path,
) -> None:
    """命中、无 fid、列表也没有：直接失败提示刷新，避免重复上传产生副本。"""
    local_file = tmp_path / "movie.mkv"
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [_quick_hit_response(1)], [_success_response({"files": []})]
    )

    with pytest.raises(WopanResponseError, match="秒传已完成但目标目录未找到对应文件"):
        client.upload_file("folder-1", local_file, quick_transfer=True)

    assert upload_requests == []


def test_upload_file_quick_transfer_retries_transient_probe_errors(tmp_path: Path) -> None:
    """官方契约：瞬时失败重试（共 3 次尝试），前两次 500 第三次命中即秒传完成。"""
    local_file = tmp_path / "movie.mkv"
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [
            httpx.Response(500, json={}),
            httpx.Response(500, json={}),
            _quick_hit_response(1, fid="fid-qt"),
        ],
        [],
    )

    item = client.upload_file("folder-1", local_file, quick_transfer=True)

    assert item.item_id == "fid-qt"
    assert len(quick_payloads) == 3  # 三次尝试各带同一份哈希载荷
    assert upload_requests == []


def test_upload_file_quick_transfer_recovery_listing_error_raises_friendly(
    tmp_path: Path,
) -> None:
    """命中无 fid 且恢复列表 5xx：转为友好报错，绝不冒泡 5xx 触发整文件重传。"""
    local_file = tmp_path / "movie.mkv"
    local_file.write_bytes(QUICK_TRANSFER_CONTENT)
    client, quick_payloads, upload_requests = _upload_flow_client(
        [_quick_hit_response(1)], [httpx.Response(502, json={})]
    )

    with pytest.raises(WopanResponseError, match="秒传已完成但目标目录未找到对应文件"):
        client.upload_file("folder-1", local_file, quick_transfer=True)

    assert upload_requests == []


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
        (lambda client: client.delete_many([]), "items"),
        (lambda client: client.delete_many([("", WopanItemKind.FILE)]), "item_id"),
        (lambda client: client.move("", WopanItemKind.FILE, "0"), "item_id"),
        (lambda client: client.move("item-1", WopanItemKind.FILE, ""), "target_parent_id"),
        (lambda client: client.move_many([], "0"), "items"),
        (
            lambda client: client.move_many([("item-1", WopanItemKind.FILE)], ""),
            "target_parent_id",
        ),
        (
            lambda client: client.move_many([("", WopanItemKind.FILE)], "0"),
            "item_id",
        ),
        (lambda client: client.copy("", WopanItemKind.FILE, "0"), "item_id"),
        (lambda client: client.copy("item-1", WopanItemKind.FILE, ""), "target_parent_id"),
        (lambda client: client.copy_many([], "0"), "items"),
        (
            lambda client: client.copy_many([("item-1", WopanItemKind.FILE)], ""),
            "target_parent_id",
        ),
        (
            lambda client: client.copy_many([("", WopanItemKind.FILE)], "0"),
            "item_id",
        ),
        (lambda client: client.search_files(""), "keyword"),
        (lambda client: client.search_files("   "), "keyword"),
        (lambda client: client.search_files("kw", 0), "page_no"),
        (lambda client: client.search_files("kw", 1, 0), "page_size"),
        (lambda client: client.get_directory_path(""), "directory_id"),
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
