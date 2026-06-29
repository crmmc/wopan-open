from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan.client import WopanClient
from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanBusinessError,
    WopanResponseError,
)


def _json_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _pkcs7_pad(data: bytes, block_size: int) -> bytes:
    padding = block_size - len(data) % block_size
    return data + bytes([padding]) * padding


def _encrypt_api_user_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    cipher = Cipher(
        algorithms.AES(b"XFmi9GS2hzk98jGX"),
        modes.CBC(b"wNSOYIB1k1DjY5lA"),
    )
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(_pkcs7_pad(encoded, 16)) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _encrypt_api_user_payload_string(payload: str) -> str:
    encoded = payload.encode()
    cipher = Cipher(
        algorithms.AES(b"XFmi9GS2hzk98jGX"),
        modes.CBC(b"wNSOYIB1k1DjY5lA"),
    )
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(_pkcs7_pad(encoded, 16)) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def test_validate_session_maps_app_query_user_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["Accesstoken"] == "token-value"
        assert body["header"]["key"] == "AppQueryUser"
        assert body["header"]["channel"] == "api-user"
        return _json_response(
            {
                "STATUS": "200",
                "MSG": "ok",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": {
                        "userId": "user-1",
                        "userName": "User One",
                    },
                },
            }
        )

    client = WopanClient(
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    user = client.validate_session("token-value")

    assert user.account_id == "user-1"
    assert user.display_name == "User One"


def test_validate_session_maps_login_expired() -> None:
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
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanAuthenticationError, match="login expired"):
        client.validate_session("token-value")


def test_validate_session_maps_business_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "9999",
                    "RSP_DESC": "business failed",
                    "DATA": "",
                },
            }
        )

    client = WopanClient(
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanBusinessError) as exc_info:
        client.validate_session("token-value")

    assert exc_info.value.code == "9999"
    assert exc_info.value.message == "business failed"


def test_validate_session_rejects_missing_user_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": {"userName": "User One"},
                },
            }
        )

    client = WopanClient(
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanResponseError, match="userId"):
        client.validate_session("token-value")


def test_validate_session_sends_encrypted_param_string_and_decrypts_response() -> None:
    captured_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = json.loads(request.content)
        data = _encrypt_api_user_payload({"userId": "user-1", "userName": "User One"})
        return _json_response(
            {
                "STATUS": "200",
                "MSG": "ok",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": data,
                },
            }
        )

    client = WopanClient(
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    user = client.validate_session("token-value")

    body = captured_body["body"]
    assert isinstance(body, dict)
    assert body["secret"] is True
    assert body["clientId"] == "1001000021"
    assert isinstance(body["param"], str)
    assert "token-value" not in body["param"]
    assert user.account_id == "user-1"
    assert user.display_name == "User One"


def test_validate_session_maps_invalid_encrypted_data_to_response_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": "not-valid-base64",
                },
            }
        )

    client = WopanClient(
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanResponseError, match="encrypted DATA"):
        client.validate_session("token-value")


def test_validate_session_maps_encrypted_non_json_to_response_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        data = _encrypt_api_user_payload_string("not-json")
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": data,
                },
            }
        )

    client = WopanClient(
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanResponseError, match="encrypted DATA"):
        client.validate_session("token-value")


def test_validate_session_maps_invalid_padding_to_response_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        data = base64.b64encode(b"0" * 16).decode("ascii")
        return _json_response(
            {
                "STATUS": "200",
                "RSP": {
                    "RSP_CODE": "0000",
                    "RSP_DESC": "success",
                    "DATA": data,
                },
            }
        )

    client = WopanClient(
        "WoCloud-Web-Token=token-value",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WopanResponseError, match="encrypted DATA"):
        client.validate_session("token-value")
