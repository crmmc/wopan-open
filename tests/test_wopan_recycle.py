from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan.client import WopanClient
from openwopan.wopan.errors import WopanResponseError
from openwopan.wopan.models import WopanItemKind

TOKEN = "1234567890abcdef-token"
COOKIE_HEADER = f"foo=bar; WoCloud-Web-Token={TOKEN}"


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


def _success_response(data: object) -> httpx.Response:
    response_data = data if isinstance(data, str) else _encrypt_wohome_payload(data)
    return httpx.Response(
        200,
        json={
            "STATUS": "200",
            "MSG": "ok",
            "RSP": {
                "RSP_CODE": "0000",
                "RSP_DESC": "success",
                "DATA": response_data,
            },
        },
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


def _recycle_raw(delete_no: str, **extra: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "deleteNo": delete_no,
        "id": f"item-{delete_no}",
        "name": f"{delete_no}.txt",
        "type": "1",
    }
    raw.update(extra)
    return raw


# -- list_recycle_items ---------------------------------------------------------


def test_list_recycle_items_maps_fields_and_requests_first_page() -> None:
    client, captured = _client_and_captured_params(
        [
            _success_response(
                [
                    _recycle_raw(
                        "d-1",
                        name="report.txt",
                        type="1",
                        fileType="4",
                        fileSize="2048",
                        keepDays="30",
                        deleteTime="20260626010203",
                    ),
                    _recycle_raw("d-2", name="Docs", type="0", fileSize="0"),
                ]
            )
        ]
    )

    items = client.list_recycle_items()

    assert captured == [
        (
            "QueryRecycleData",
            {
                "pageNo": 1,
                "pageSize": 100,
                "sortRule": 6,
                "clientId": "1001000021",
            },
        )
    ]
    assert [item.delete_no for item in items] == ["d-1", "d-2"]
    assert items[0].item_id == "item-d-1"
    assert items[0].name == "report.txt"
    assert items[0].kind is WopanItemKind.FILE
    assert items[0].file_type == "4"
    assert items[0].size == 2048
    assert items[0].keep_days == 30
    assert items[0].deleted_at is not None
    assert items[0].deleted_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-06-26 01:02:03"
    assert items[1].kind is WopanItemKind.FOLDER
    assert items[1].size == 0
    assert items[1].keep_days is None
    assert items[1].deleted_at is None


def test_list_recycle_item_defaults_missing_fields() -> None:
    client, _captured = _client_and_captured_params(
        [_success_response([_recycle_raw("d-1")])]
    )

    items = client.list_recycle_items()

    assert items[0].file_type is None
    assert items[0].size is None
    assert items[0].keep_days is None
    assert items[0].deleted_at is None


def test_list_recycle_item_falls_back_to_fid_for_item_id() -> None:
    raw = _recycle_raw("d-1")
    del raw["id"]
    raw["fid"] = "fid-1"
    client, _captured = _client_and_captured_params([_success_response([raw])])

    items = client.list_recycle_items()

    assert items[0].item_id == "fid-1"


def test_list_recycle_item_degrades_unparsable_delete_time() -> None:
    client, _captured = _client_and_captured_params(
        [_success_response([_recycle_raw("d-1", deleteTime="not-a-date")])]
    )

    items = client.list_recycle_items()

    assert items[0].deleted_at is None


def test_list_recycle_items_stops_on_short_page() -> None:
    client, captured = _client_and_captured_params(
        [_success_response([_recycle_raw("d-1"), _recycle_raw("d-2")])]
    )

    items = client.list_recycle_items()

    assert [item.delete_no for item in items] == ["d-1", "d-2"]
    assert [key for key, _param in captured] == ["QueryRecycleData"]


def test_list_recycle_items_returns_empty_list_for_empty_bin() -> None:
    client, captured = _client_and_captured_params([_success_response([])])

    items = client.list_recycle_items()

    assert items == []
    assert len(captured) == 1


def test_list_recycle_items_stops_on_repeated_page() -> None:
    # The server may ignore pageNo and echo the same full page forever; every
    # deleteNo on page 2 is already seen, so paging must stop after two calls.
    page = [_recycle_raw(f"d-{index}") for index in range(100)]
    client, captured = _client_and_captured_params(
        [_success_response(page), _success_response(page)]
    )

    items = client.list_recycle_items()

    assert len(items) == 100
    assert [param["pageNo"] for _key, param in captured] == [1, 2]


def test_list_recycle_items_collects_across_pages_until_max_items() -> None:
    page_one = [_recycle_raw(f"a-{index}") for index in range(100)]
    page_two = [_recycle_raw(f"b-{index}") for index in range(100)]
    client, captured = _client_and_captured_params(
        [_success_response(page_one), _success_response(page_two)]
    )

    items = client.list_recycle_items(max_items=150)

    assert len(items) == 150
    assert items[99].delete_no == "a-99"
    assert items[100].delete_no == "b-0"
    assert items[149].delete_no == "b-49"
    assert [param["pageNo"] for _key, param in captured] == [1, 2]


def test_list_recycle_items_dedupes_repeated_delete_no() -> None:
    client, _captured = _client_and_captured_params(
        [
            _success_response(
                [_recycle_raw("d-1"), _recycle_raw("d-1"), _recycle_raw("d-2")]
            )
        ]
    )

    items = client.list_recycle_items()

    assert [item.delete_no for item in items] == ["d-1", "d-2"]


def test_list_recycle_items_rejects_non_positive_max_items() -> None:
    client, captured = _client_and_captured_params([_success_response([])])

    with pytest.raises(ValueError, match="max_items"):
        client.list_recycle_items(max_items=0)

    assert captured == []


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"not": "a list"}, "DATA is not a list"),
        (["str-item"], "item is not an object"),
    ],
    ids=["data-not-list", "item-not-object"],
)
def test_list_recycle_items_rejects_malformed_payloads(data: object, match: str) -> None:
    client, _captured = _client_and_captured_params([_success_response(data)])

    with pytest.raises(WopanResponseError, match=match):
        client.list_recycle_items()


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (_recycle_raw("d-1", deleteNo=""), "missing deleteNo"),
        (_recycle_raw("d-1", name=""), "missing name"),
        ({**_recycle_raw("d-1"), "id": "", "fid": ""}, "missing id"),
        (_recycle_raw("d-1", type="7"), "unknown type"),
    ],
    ids=["no-delete-no", "no-name", "no-id", "unknown-type"],
)
def test_list_recycle_items_rejects_malformed_items(
    raw: dict[str, object], match: str
) -> None:
    client, _captured = _client_and_captured_params([_success_response([raw])])

    with pytest.raises(WopanResponseError, match=match):
        client.list_recycle_items()


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ({"fileSize": "abc"}, "fileSize is not an integer"),
        ({"fileSize": "-1"}, "fileSize is negative"),
        ({"keepDays": "abc"}, "keepDays is not an integer"),
    ],
    ids=["file-size-text", "file-size-negative", "keep-days-text"],
)
def test_list_recycle_items_rejects_malformed_numbers(
    extra: dict[str, object], match: str
) -> None:
    client, _captured = _client_and_captured_params(
        [_success_response([_recycle_raw("d-1", **extra)])]
    )

    with pytest.raises(WopanResponseError, match=match):
        client.list_recycle_items()


# -- restore / purge / empty ----------------------------------------------------


def test_restore_recycle_items_sends_delete_no_batch() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.restore_recycle_items(["d-1", "d-2"])

    assert captured == [
        (
            "ReductionRecycleData",
            {
                "deleteNos": ["d-1", "d-2"],
                "deviceNo": "1001000021",
                "clientId": "1001000021",
            },
        )
    ]


def test_purge_recycle_items_sends_delete_no_batch() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.purge_recycle_items(["d-1", "d-2"])

    assert captured == [
        (
            "DeleteRecycleData",
            {
                "deleteNos": ["d-1", "d-2"],
                "clientId": "1001000021",
            },
        )
    ]


def test_empty_recycle_bin_sends_client_id() -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    client.empty_recycle_bin()

    assert captured == [
        (
            "EmptyRecycleData",
            {
                "clientId": "1001000021",
            },
        )
    ]


@pytest.mark.parametrize(
    "action",
    [
        lambda client: client.restore_recycle_items([]),
        lambda client: client.restore_recycle_items([""]),
        lambda client: client.restore_recycle_items(["d-1", ""]),
        lambda client: client.purge_recycle_items([]),
        lambda client: client.purge_recycle_items([""]),
        lambda client: client.purge_recycle_items(["d-1", ""]),
    ],
    ids=[
        "restore-empty",
        "restore-blank",
        "restore-trailing-blank",
        "purge-empty",
        "purge-blank",
        "purge-trailing-blank",
    ],
)
def test_recycle_mutations_reject_empty_inputs(action: object) -> None:
    client, captured = _client_and_captured_params([_success_response("")])

    with pytest.raises(ValueError):
        action(client)  # type: ignore[operator]

    assert captured == []
