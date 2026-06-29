from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from openwopan.auth.web_login import WebLoginTokenError, extract_wocloud_token
from openwopan.storage.credentials import CredentialStore
from openwopan.wopan.client import (
    CHANNEL_WOCLOUD,
    DEFAULT_UPLOAD_ZONE_URL,
    ORIGIN,
    PERSONAL_SPACE_TYPE,
    REFERER,
    ROOT_DIRECTORY_ID,
    STANDARD_BROWSER_USER_AGENT,
    WopanClient,
    _encrypt_param,
    _wohome_crypto_key,
)
from openwopan.wopan.models import WopanItem, WopanItemKind

LIVE_TEST_ENV = "OPENWOPAN_LIVE_TRANSFER_TEST"
PROBE_PREFIX = "openwopan_live_probe"
DEFAULT_PART_SIZE = 8 * 1024 * 1024

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_TEST_ENV) != "1",
    reason=f"set {LIVE_TEST_ENV}=1 to run live WoPan transfer capability tests",
)


@dataclass(slots=True)
class LiveWopanContext:
    client: WopanClient
    cookie_header: str = field(repr=False)
    token: str = field(repr=False)
    created_names: list[str] = field(default_factory=list)


@pytest.fixture
def live_wopan() -> Iterator[LiveWopanContext]:
    store = CredentialStore()
    account_id = store.get_last_account_id()
    if account_id is None:
        pytest.skip("no saved OpenWoPan account id in keyring")

    cookie_header = store.get_session_cookie(account_id)
    if cookie_header is None:
        pytest.skip("no saved OpenWoPan session cookie in keyring")

    try:
        token = extract_wocloud_token(cookie_header)
    except WebLoginTokenError as exc:
        pytest.fail(f"saved OpenWoPan session cookie is invalid: {exc}")

    context = LiveWopanContext(
        client=WopanClient(cookie_header),
        cookie_header=cookie_header,
        token=token,
    )
    yield context
    _cleanup_probe_files(context)


def test_live_saved_cookie_can_validate_session(live_wopan: LiveWopanContext) -> None:
    user = live_wopan.client.validate_session(live_wopan.token)

    assert user.account_id


def test_live_download_url_supports_range(
    live_wopan: LiveWopanContext,
    tmp_path: Path,
) -> None:
    content = _probe_content("range", 64 * 1024)
    remote_item = _upload_single_part_probe(
        live_wopan,
        tmp_path,
        file_name=_probe_name("range", "bin"),
        content=content,
    )

    download_id = _require_download_id(remote_item)
    download_url = live_wopan.client.get_download_info(download_id).url
    headers = _download_headers({"Range": "bytes=0-1023"})

    with httpx.Client(follow_redirects=True, timeout=60.0) as http_client:
        head_summary = _summarize_head_response(http_client, download_url)
        response = http_client.get(download_url, headers=headers)

    assert response.status_code == 206, _range_failure_message(response, head_summary)
    assert response.headers.get("Content-Range", "").startswith("bytes 0-1023/")
    assert response.content == content[:1024]


def test_live_upload2c_accepts_serial_multipart(live_wopan: LiveWopanContext) -> None:
    content = _multipart_probe_content("serial")
    file_name = _probe_name("serial-multipart", "bin")

    remote_item = _upload_multipart_probe(
        live_wopan,
        file_name=file_name,
        content=content,
        parallel=False,
    )

    downloaded = _download_item_content(live_wopan, remote_item)
    assert _sha256(downloaded) == _sha256(content)


def test_live_upload2c_accepts_parallel_multipart(live_wopan: LiveWopanContext) -> None:
    content = _multipart_probe_content("parallel")
    file_name = _probe_name("parallel-multipart", "bin")

    remote_item = _upload_multipart_probe(
        live_wopan,
        file_name=file_name,
        content=content,
        parallel=True,
    )

    downloaded = _download_item_content(live_wopan, remote_item)
    assert _sha256(downloaded) == _sha256(content)


def _upload_single_part_probe(
    context: LiveWopanContext,
    tmp_path: Path,
    *,
    file_name: str,
    content: bytes,
) -> WopanItem:
    context.created_names.append(file_name)
    local_path = tmp_path / file_name
    local_path.write_bytes(content)

    context.client.upload_file(ROOT_DIRECTORY_ID, local_path)
    return _find_created_file(context, file_name)


def _upload_multipart_probe(
    context: LiveWopanContext,
    *,
    file_name: str,
    content: bytes,
    parallel: bool,
) -> WopanItem:
    context.created_names.append(file_name)
    parts = _sdk_style_parts(content)
    zone_url = context.client.get_upload_zone_url() or DEFAULT_UPLOAD_ZONE_URL
    upload_url = f"{zone_url.rstrip('/')}/openapi/client/upload2C"
    token_key = _wohome_crypto_key(context.token)
    file_info = {
        "spaceType": PERSONAL_SPACE_TYPE,
        "directoryId": ROOT_DIRECTORY_ID,
        "batchNo": time.strftime("%Y%m%d%H%M%S"),
        "fileName": file_name,
        "fileSize": len(content),
        "fileType": "0",
    }
    base_form_data = {
        "uniqueId": str(int(time.time() * 1000)),
        "accessToken": context.token,
        "fileName": file_name,
        "psToken": "undefined",
        "fileSize": str(len(content)),
        "totalPart": str(len(parts)),
        "channel": CHANNEL_WOCLOUD,
        "directoryId": ROOT_DIRECTORY_ID,
        "fileInfo": _encrypt_param(file_info, token_key),
    }

    if parallel:
        with ThreadPoolExecutor(max_workers=len(parts)) as executor:
            futures = [
                executor.submit(
                    _post_upload_part,
                    upload_url,
                    base_form_data,
                    file_name,
                    part,
                    part_index,
                )
                for part_index, part in enumerate(parts, start=1)
            ]
            for future in as_completed(futures):
                future.result()
    else:
        for part_index, part in enumerate(parts, start=1):
            _post_upload_part(upload_url, base_form_data, file_name, part, part_index)

    return _find_created_file(context, file_name)


def _post_upload_part(
    upload_url: str,
    base_form_data: dict[str, str],
    file_name: str,
    content: bytes,
    part_index: int,
) -> None:
    form_data = {
        **base_form_data,
        "partSize": str(len(content)),
        "partIndex": str(part_index),
    }
    headers = {
        "Origin": ORIGIN,
        "Referer": REFERER,
        "User-Agent": STANDARD_BROWSER_USER_AGENT,
    }
    with httpx.Client(timeout=120.0) as http_client:
        response = http_client.post(
            upload_url,
            headers=headers,
            data=form_data,
            files={"file": (file_name, content, "application/octet-stream")},
        )
    response.raise_for_status()
    raw = response.json()
    if not isinstance(raw, dict):
        raise AssertionError(f"upload2C part {part_index} response is not an object")

    code = str(raw.get("code") or "")
    if code != "0000":
        message = _safe_text(raw.get("msg"))
        raise AssertionError(f"upload2C part {part_index} failed code={code} msg={message}")


def _find_created_file(context: LiveWopanContext, file_name: str) -> WopanItem:
    deadline = time.monotonic() + 30.0
    while True:
        for item in context.client.list_files(ROOT_DIRECTORY_ID):
            if item.name == file_name and item.kind is WopanItemKind.FILE:
                return item
        if time.monotonic() >= deadline:
            raise AssertionError(f"uploaded probe file did not appear: {file_name}")
        time.sleep(2.0)


def _download_item_content(context: LiveWopanContext, item: WopanItem) -> bytes:
    download_id = _require_download_id(item)
    download_url = context.client.get_download_info(download_id).url
    with httpx.Client(follow_redirects=True, timeout=120.0) as http_client:
        response = http_client.get(download_url, headers=_download_headers())
    assert response.status_code == 200, _download_failure_message(response)
    return response.content


def _require_download_id(item: WopanItem) -> str:
    if not item.download_id:
        raise AssertionError(f"probe file is missing download_id: {item.name}")
    return item.download_id


def _cleanup_probe_files(context: LiveWopanContext) -> None:
    if not context.created_names:
        return
    names = set(context.created_names)
    with suppress(Exception):
        for item in context.client.list_files(ROOT_DIRECTORY_ID):
            if item.name in names and item.kind is WopanItemKind.FILE:
                with suppress(Exception):
                    context.client.delete(item.item_id, item.kind)


def _sdk_style_parts(content: bytes) -> list[bytes]:
    total_part = len(content) // DEFAULT_PART_SIZE
    if total_part < 2:
        raise ValueError("multipart probe content must produce at least two SDK-style parts")

    parts: list[bytes] = []
    finished_size = 0
    for part_index in range(1, total_part + 1):
        part_size = DEFAULT_PART_SIZE
        if part_index == total_part:
            part_size = len(content) - finished_size
        parts.append(content[finished_size : finished_size + part_size])
        finished_size += part_size
    assert finished_size == len(content)
    return parts


def _multipart_probe_content(label: str) -> bytes:
    return _probe_content(label, DEFAULT_PART_SIZE * 2 + 4096)


def _probe_content(label: str, size: int) -> bytes:
    pattern = f"openwopan-live-probe:{label}:".encode()
    repeats = size // len(pattern) + 1
    return (pattern * repeats)[:size]


def _probe_name(case_name: str, suffix: str) -> str:
    return f"{PROBE_PREFIX}_{int(time.time() * 1000)}_{case_name}.{suffix}"


def _download_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Referer": REFERER,
        "User-Agent": STANDARD_BROWSER_USER_AGENT,
    }
    if extra:
        headers.update(extra)
    return headers


def _summarize_head_response(http_client: httpx.Client, download_url: str) -> dict[str, str]:
    try:
        response = http_client.head(download_url, headers=_download_headers())
    except httpx.HTTPError as exc:
        return {"head_error": type(exc).__name__}
    return {
        "head_status": str(response.status_code),
        "head_accept_ranges": response.headers.get("Accept-Ranges", ""),
        "head_content_length": response.headers.get("Content-Length", ""),
    }


def _range_failure_message(response: httpx.Response, head_summary: dict[str, str]) -> str:
    details = {
        **head_summary,
        "get_status": str(response.status_code),
        "get_accept_ranges": response.headers.get("Accept-Ranges", ""),
        "get_content_range": response.headers.get("Content-Range", ""),
        "get_content_length": response.headers.get("Content-Length", ""),
        "body_length": str(len(response.content)),
    }
    return f"download URL did not prove Range support: {details}"


def _download_failure_message(response: httpx.Response) -> str:
    details = {
        "status": str(response.status_code),
        "content_length": response.headers.get("Content-Length", ""),
        "body_length": str(len(response.content)),
    }
    return f"probe file download failed: {details}"


def _safe_text(value: Any) -> str:
    text = str(value or "")
    if len(text) > 120:
        return f"{text[:117]}..."
    return text


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
