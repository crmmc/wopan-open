from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import mimetypes
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from secrets import randbelow
from typing import Any
from urllib.parse import unquote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openwopan.wopan.errors import (
    WopanAuthenticationError,
    WopanBusinessError,
    WopanResponseError,
)
from openwopan.wopan.models import DownloadInfo, WopanCloudUsage, WopanItem, WopanItemKind

BASE_URL = "https://panservice.mail.wo.cn"
CLIENT_ID = "1001000021"
CLIENT_SECRET = "XFmi9GS2hzk98jGX"
IV = "wNSOYIB1k1DjY5lA"
ORIGIN = "https://pan.wo.cn"
REFERER = "https://pan.wo.cn/"
CHANNEL_API_USER = "api-user"
CHANNEL_WOHOME = "wohome"
CHANNEL_WOCLOUD = "wocloud"
ROOT_DIRECTORY_ID = "0"
DEFAULT_PAGE_SIZE = 100
DEFAULT_SORT_RULE = 6
DEFAULT_UPLOAD_APP_ID = "10000001"
DEFAULT_UPLOAD_ZONE_URL = "https://tjupload.pan.wo.cn"
BYTES_PER_MB = 1024 * 1024
TOKEN_COOKIE_NAME = "WoCloud-Web-Token"
PERSONAL_SPACE_TYPE = "0"
PERSONAL_FAMILY_ID = "0"
DEFAULT_VIP_LEVEL = "0"
STANDARD_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/114.0.0.0 Safari/537.36 Edg/114.0.1823.37"
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ValidatedWopanUser:
    """Validated WoPan user summary returned by the protocol layer."""

    account_id: str
    display_name: str | None = None


class WopanClient:
    """Protocol-layer boundary for future WoPan HTTP API calls."""

    def __init__(self, cookie_header: str, http_client: httpx.Client | None = None) -> None:
        if not cookie_header:
            raise ValueError("cookie_header must not be empty")
        self._cookie_header = cookie_header
        self._access_token = _extract_token_from_cookie_header(cookie_header)
        self._http_client = http_client or httpx.Client(
            headers={"Origin": ORIGIN, "Referer": REFERER},
            follow_redirects=True,
            timeout=30.0,
        )

    def validate_session(self, token: str) -> ValidatedWopanUser:
        """Validate the current login state with AppQueryUser."""
        if not token:
            raise ValueError("token must not be empty")
        LOGGER.info("wopan.validate_session.start")
        data = self._dispatch_api_user("AppQueryUser", {"accessToken": token}, token)
        account_id = str(data.get("userId") or "")
        if not account_id:
            raise WopanResponseError("AppQueryUser response missing userId")
        display_name = data.get("userName")
        LOGGER.info("wopan.validate_session.success account_id_present=%s", bool(account_id))
        return ValidatedWopanUser(
            account_id=account_id,
            display_name=str(display_name) if display_name else None,
        )

    def query_cloud_usage(self, account_id: str) -> WopanCloudUsage:
        """Query current personal cloud storage usage."""
        if not account_id:
            raise ValueError("account_id must not be empty")

        LOGGER.info("wopan.query_cloud_usage.start account_id_present=%s", bool(account_id))
        data = self._dispatch_wohome(
            "QueryCloudUsageInfo",
            {
                "phoneNum": account_id,
                "clientId": CLIENT_ID,
            },
        )
        usage_info = data.get("usageInfo")
        if not isinstance(usage_info, dict):
            raise WopanResponseError("QueryCloudUsageInfo response missing usageInfo")
        usage = WopanCloudUsage(
            used_bytes=_read_required_non_negative_int(
                usage_info.get("byteUsedSize"),
                "QueryCloudUsageInfo usageInfo.byteUsedSize",
            ),
            total_bytes=_read_required_positive_int(
                usage_info.get("byteTotalSize"),
                "QueryCloudUsageInfo usageInfo.byteTotalSize",
            ),
            vip_level=_read_optional_text(data.get("vipLevel")),
            expire_time=_read_optional_text(data.get("expireTime")),
        )
        LOGGER.info(
            "wopan.query_cloud_usage.success used_bytes=%s total_bytes=%s",
            usage.used_bytes,
            usage.total_bytes,
        )
        return usage

    def _dispatch_api_user(self, key: str, param: dict[str, Any], token: str) -> dict[str, Any]:
        now = int(time.time() * 1000)
        seq = randbelow(8999) + 100_000
        payload = {
            "header": {
                "key": key,
                "resTime": now,
                "reqSeq": seq,
                "channel": CHANNEL_API_USER,
                "sign": _sign(key, now, seq, CHANNEL_API_USER),
                "version": "",
            },
            "body": {
                "secret": True,
                "clientId": CLIENT_ID,
                "param": _encrypt_param(param, CLIENT_SECRET),
            },
        }
        return self._post_dispatch(
            channel=CHANNEL_API_USER,
            key=key,
            payload=payload,
            headers={"Content-Type": "application/json", "Accesstoken": token},
            decrypt_key=CLIENT_SECRET,
        )

    def _dispatch_wohome(
        self,
        key: str,
        param: dict[str, Any],
        *,
        body_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = self._dispatch_wohome_payload(key, param, body_extra=body_extra)
        if not isinstance(data, dict):
            raise WopanResponseError("WoPan response DATA is not an object")
        return data

    def _dispatch_wohome_payload(
        self,
        key: str,
        param: dict[str, Any],
        *,
        body_extra: dict[str, Any] | None = None,
    ) -> Any:
        token_key = _wohome_crypto_key(self._access_token)
        now = int(time.time() * 1000)
        seq = randbelow(8999) + 100_000
        body = {
            "secret": True,
            "param": _encrypt_param(param, token_key),
        }
        if body_extra:
            body.update(body_extra)
        payload = {
            "header": {
                "key": key,
                "resTime": now,
                "reqSeq": seq,
                "channel": CHANNEL_WOHOME,
                "sign": _sign(key, now, seq, CHANNEL_WOHOME),
                "version": "",
            },
            "body": body,
        }
        return self._post_dispatch_payload(
            channel=CHANNEL_WOHOME,
            key=key,
            payload=payload,
            headers={"Content-Type": "application/json", "Accesstoken": self._access_token},
            decrypt_key=token_key,
        )

    def _post_dispatch(
        self,
        *,
        channel: str,
        key: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        decrypt_key: str,
    ) -> dict[str, Any]:
        data = self._post_dispatch_payload(
            channel=channel,
            key=key,
            payload=payload,
            headers=headers,
            decrypt_key=decrypt_key,
        )
        if not isinstance(data, dict):
            raise WopanResponseError("WoPan response DATA is not an object")
        LOGGER.debug(
            "wopan.dispatch.success channel=%s key=%s data_keys=%s",
            channel,
            key,
            sorted(data),
        )
        return data

    def _post_dispatch_payload(
        self,
        *,
        channel: str,
        key: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        decrypt_key: str,
    ) -> Any:
        LOGGER.debug("wopan.dispatch.start channel=%s key=%s", channel, key)
        try:
            response = self._http_client.post(
                f"{BASE_URL}/{channel}/dispatcher",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, dict):
                raise WopanResponseError("WoPan response is not an object")
            data = _read_dispatch_payload(raw, decrypt_key)
        except WopanAuthenticationError:
            LOGGER.info("wopan.dispatch.auth_failed channel=%s key=%s", channel, key)
            raise
        except WopanBusinessError as exc:
            LOGGER.warning(
                "wopan.dispatch.business_error channel=%s key=%s code=%s message=%s",
                channel,
                key,
                exc.code,
                exc.message,
            )
            raise
        except WopanResponseError:
            LOGGER.warning("wopan.dispatch.response_error channel=%s key=%s", channel, key)
            raise
        except httpx.HTTPError:
            LOGGER.warning("wopan.dispatch.http_error channel=%s key=%s", channel, key)
            raise
        return data

    def list_files(self, parent_id: str) -> list[WopanItem]:
        """List files and folders under a parent directory."""
        if not parent_id:
            raise ValueError("parent_id must not be empty")

        LOGGER.info("wopan.list_files.start parent_id=%s", parent_id)
        data = self._dispatch_wohome(
            "QueryAllFiles",
            {
                "spaceType": PERSONAL_SPACE_TYPE,
                "parentDirectoryId": parent_id,
                "pageNum": 0,
                "pageSize": DEFAULT_PAGE_SIZE,
                "sortRule": DEFAULT_SORT_RULE,
                "clientId": CLIENT_ID,
            },
        )
        items: list[WopanItem] = []
        skipped_count = 0
        for field_name in ("systemDirs", "files"):
            raw_items = data.get(field_name, [])
            if raw_items is None:
                continue
            if not isinstance(raw_items, list):
                raise WopanResponseError(f"QueryAllFiles {field_name} is not a list")
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    raise WopanResponseError("QueryAllFiles item is not an object")
                if _read_wopan_item_type(raw_item) not in ("0", "1"):
                    skipped_count += 1
                    _log_skipped_unknown_type_item(
                        raw_item,
                        parent_id=parent_id,
                        field_name=field_name,
                    )
                    continue
                items.append(_read_wopan_item(raw_item, fallback_parent_id=parent_id))
        LOGGER.info(
            "wopan.list_files.success parent_id=%s item_count=%s skipped_count=%s",
            parent_id,
            len(items),
            skipped_count,
        )
        return items

    def create_folder(self, parent_id: str, name: str) -> WopanItem:
        """Create a folder under a parent directory."""
        if not parent_id:
            raise ValueError("parent_id must not be empty")
        if not name:
            raise ValueError("name must not be empty")

        LOGGER.info("wopan.create_folder.start parent_id=%s name_length=%s", parent_id, len(name))
        data = self._dispatch_wohome(
            "CreateDirectory",
            {
                "spaceType": PERSONAL_SPACE_TYPE,
                "familyId": PERSONAL_FAMILY_ID,
                "parentDirectoryId": parent_id,
                "directoryName": name,
                "clientId": CLIENT_ID,
            },
        )
        item_id = str(data.get("id") or "")
        if not item_id:
            raise WopanResponseError("CreateDirectory response missing id")
        LOGGER.info("wopan.create_folder.success parent_id=%s item_id=%s", parent_id, item_id)
        return WopanItem(
            item_id=item_id,
            name=name,
            kind=WopanItemKind.FOLDER,
            parent_id=parent_id,
            file_type="0",
        )

    def rename(
        self,
        item_id: str,
        new_name: str,
        kind: WopanItemKind,
        file_type: str | None = None,
    ) -> None:
        """Rename a file or folder."""
        if not item_id:
            raise ValueError("item_id must not be empty")
        if not new_name:
            raise ValueError("new_name must not be empty")

        LOGGER.info(
            "wopan.rename.start item_id=%s kind=%s name_length=%s",
            item_id,
            kind,
            len(new_name),
        )
        self._dispatch_wohome(
            "RenameFileOrDirectory",
            {
                "spaceType": PERSONAL_SPACE_TYPE,
                "type": _wopan_kind_value(kind),
                "fileType": file_type or "0",
                "id": item_id,
                "name": new_name,
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info("wopan.rename.success item_id=%s kind=%s", item_id, kind)

    def delete(self, item_id: str, kind: WopanItemKind) -> None:
        """Delete a file or folder."""
        if not item_id:
            raise ValueError("item_id must not be empty")

        LOGGER.info("wopan.delete.start item_id=%s kind=%s", item_id, kind)
        self._dispatch_wohome(
            "DeleteFile",
            {
                "spaceType": PERSONAL_SPACE_TYPE,
                "vipLevel": DEFAULT_VIP_LEVEL,
                "dirList": [item_id] if kind is WopanItemKind.FOLDER else [],
                "fileList": [item_id] if kind is WopanItemKind.FILE else [],
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info("wopan.delete.success item_id=%s kind=%s", item_id, kind)

    def move(self, item_id: str, kind: WopanItemKind, target_parent_id: str) -> None:
        """Move a file or folder to another parent directory."""
        if not item_id:
            raise ValueError("item_id must not be empty")
        if not target_parent_id:
            raise ValueError("target_parent_id must not be empty")

        LOGGER.info(
            "wopan.move.start item_id=%s kind=%s target_parent_id=%s",
            item_id,
            kind,
            target_parent_id,
        )
        self._dispatch_wohome(
            "MoveFile",
            {
                "targetDirId": target_parent_id,
                "sourceType": PERSONAL_SPACE_TYPE,
                "targetType": PERSONAL_SPACE_TYPE,
                "dirList": [item_id] if kind is WopanItemKind.FOLDER else [],
                "fileList": [item_id] if kind is WopanItemKind.FILE else [],
                "secret": False,
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info(
            "wopan.move.success item_id=%s kind=%s target_parent_id=%s",
            item_id,
            kind,
            target_parent_id,
        )

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_part_size_mb: int = 5,
        max_upload_threads: int = 16,
        retry_max_attempts: int = 3,
    ) -> WopanItem:
        """Upload a local file to a parent directory."""
        if not parent_id:
            raise ValueError("parent_id must not be empty")
        if not local_path.is_file():
            raise ValueError("local_path must be an existing file")

        file_size = local_path.stat().st_size
        file_name = local_path.name
        part_size = _bounded_int(upload_part_size_mb, 5, 5, 16) * BYTES_PER_MB
        total_parts = max(1, (file_size + part_size - 1) // part_size)
        max_workers = 1 if total_parts == 1 else min(
            _bounded_int(max_upload_threads, 16, 1, 16),
            total_parts,
        )
        max_attempts = _bounded_int(retry_max_attempts, 3, 0, 5) + 1
        upload_file_type = _guess_upload_file_type(file_name)
        zone_url = self.get_upload_zone_url()
        upload_url = f"{zone_url.rstrip('/')}/openapi/client/upload2C"
        unique_id = str(int(time.time() * 1000))
        token_key = _wohome_crypto_key(self._access_token)
        file_info = {
            "spaceType": PERSONAL_SPACE_TYPE,
            "directoryId": parent_id,
            "batchNo": time.strftime("%Y%m%d%H%M%S"),
            "fileName": file_name,
            "fileSize": file_size,
            "fileType": upload_file_type,
        }
        form_data = {
            "uniqueId": unique_id,
            "accessToken": self._access_token,
            "fileName": file_name,
            "psToken": "undefined",
            "fileSize": str(file_size),
            "totalPart": str(total_parts),
            "channel": CHANNEL_WOCLOUD,
            "directoryId": parent_id,
            "fileInfo": _encrypt_param(file_info, token_key),
        }
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        LOGGER.info(
            "wopan.upload_file.start parent_id=%s file_name_length=%s file_size=%s "
            "total_parts=%s workers=%s",
            parent_id,
            len(file_name),
            file_size,
            total_parts,
            max_workers,
        )
        try:
            if total_parts == 1:
                raw = self._upload_part(
                    upload_url,
                    form_data,
                    file_name,
                    mime_type,
                    local_path.read_bytes(),
                    part_index=1,
                    max_attempts=max_attempts,
                )
            else:
                raw = self._upload_parts_parallel(
                    upload_url,
                    form_data,
                    file_name,
                    mime_type,
                    local_path,
                    part_size,
                    total_parts,
                    max_workers,
                    max_attempts,
                )
        except httpx.HTTPError:
            LOGGER.warning("wopan.upload_file.http_error parent_id=%s", parent_id)
            raise
        except (OSError, ValueError) as exc:
            LOGGER.warning("wopan.upload_file.response_error parent_id=%s", parent_id)
            raise WopanResponseError("upload2C response cannot be decoded") from exc

        code = str(raw.get("code") or "")
        if code != "0000":
            message = str(raw.get("msg") or "WoPan upload failed")
            LOGGER.warning(
                "wopan.upload_file.business_error parent_id=%s code=%s message=%s",
                parent_id,
                code,
                message,
            )
            raise WopanBusinessError(code, message)
        data = raw.get("data")
        if not isinstance(data, dict):
            raise WopanResponseError("upload2C response data is not an object")
        fid = str(data.get("fid") or "")
        if not fid:
            raise WopanResponseError("upload2C response missing fid")
        LOGGER.info(
            "wopan.upload_file.success parent_id=%s file_name_length=%s fid_present=%s",
            parent_id,
            len(file_name),
            bool(fid),
        )
        return WopanItem(
            item_id=fid,
            name=file_name,
            kind=WopanItemKind.FILE,
            parent_id=parent_id,
            file_type=upload_file_type,
            download_id=fid,
            size=file_size,
        )

    def _upload_parts_parallel(
        self,
        upload_url: str,
        base_form_data: dict[str, str],
        file_name: str,
        mime_type: str,
        local_path: Path,
        part_size: int,
        total_parts: int,
        max_workers: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        last_raw: dict[str, Any] | None = None
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for part_index in range(1, total_parts + 1):
                offset = (part_index - 1) * part_size
                futures.append(
                    executor.submit(
                        self._upload_file_part,
                        upload_url,
                        base_form_data,
                        file_name,
                        mime_type,
                        local_path,
                        offset,
                        part_size,
                        part_index,
                        max_attempts,
                    )
                )
            for future in as_completed(futures):
                raw = future.result()
                last_raw = raw
        if last_raw is None:
            raise WopanResponseError("upload2C multipart upload produced no response")
        return last_raw

    def _upload_file_part(
        self,
        upload_url: str,
        base_form_data: dict[str, str],
        file_name: str,
        mime_type: str,
        local_path: Path,
        offset: int,
        part_size: int,
        part_index: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        with local_path.open("rb") as file_obj:
            file_obj.seek(offset)
            content = file_obj.read(part_size)
        return self._upload_part(
            upload_url,
            base_form_data,
            file_name,
            mime_type,
            content,
            part_index,
            max_attempts,
        )

    def _upload_part(
        self,
        upload_url: str,
        base_form_data: dict[str, str],
        file_name: str,
        mime_type: str,
        content: bytes,
        part_index: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        form_data = {
            **base_form_data,
            "partSize": str(len(content)),
            "partIndex": str(part_index),
        }
        last_error: Exception | None = None
        for _attempt in range(max_attempts):
            try:
                response = self._http_client.post(
                    upload_url,
                    headers={
                        "Origin": ORIGIN,
                        "Referer": REFERER,
                        "User-Agent": STANDARD_BROWSER_USER_AGENT,
                    },
                    data=form_data,
                    files={"file": (file_name, content, mime_type)},
                )
                response.raise_for_status()
                raw = response.json()
                if not isinstance(raw, dict):
                    raise WopanResponseError("upload2C response is not an object")
                code = str(raw.get("code") or "")
                if code != "0000":
                    message = str(raw.get("msg") or "WoPan upload failed")
                    raise WopanBusinessError(code, message)
                return raw
            except (httpx.HTTPError, WopanBusinessError) as exc:
                last_error = exc
            except ValueError as exc:
                raise WopanResponseError("upload2C response cannot be decoded") from exc
        if last_error is not None:
            raise last_error
        raise WopanResponseError("upload2C upload part failed")

    def get_upload_zone_url(self) -> str:
        """Return the current upload zone URL."""
        LOGGER.debug("wopan.get_upload_zone_url.start")
        data = self._dispatch_wohome(
            "GetZoneInfo",
            {"appId": DEFAULT_UPLOAD_APP_ID},
            body_extra={"key": True},
        )
        zone_url = str(data.get("url") or "").strip().rstrip("/")
        if not zone_url:
            zone_url = DEFAULT_UPLOAD_ZONE_URL
        LOGGER.debug("wopan.get_upload_zone_url.success zone_present=%s", bool(zone_url))
        return zone_url

    def get_download_info(self, download_id: str) -> DownloadInfo:
        """Get download metadata for a file."""
        if not download_id:
            raise ValueError("download_id must not be empty")

        LOGGER.info(
            "wopan.get_download_info.start download_id_present=%s download_id_length=%s",
            bool(download_id),
            len(download_id),
        )
        data = self._dispatch_wohome_payload(
            "GetDownloadUrl",
            {
                "fidList": [download_id],
                "clientId": CLIENT_ID,
                "spaceType": PERSONAL_SPACE_TYPE,
            },
        )
        if not isinstance(data, list):
            raise WopanResponseError("GetDownloadUrl DATA is not a list")

        entries: list[dict[str, Any]] = []
        for raw_item in data:
            if not isinstance(raw_item, dict):
                raise WopanResponseError("GetDownloadUrl item is not an object")
            entries.append(raw_item)
        if not entries:
            raise WopanResponseError("GetDownloadUrl response is empty")

        selected = next(
            (entry for entry in entries if str(entry.get("fid") or "") == download_id),
            None,
        )
        if selected is None:
            raise WopanResponseError("GetDownloadUrl response missing requested file")

        download_url = str(selected.get("downloadUrl") or "").strip()
        if not download_url:
            raise WopanResponseError("GetDownloadUrl response missing downloadUrl")
        LOGGER.info("wopan.get_download_info.success download_id_present=%s", bool(download_id))
        return DownloadInfo(url=download_url)


def _sign(key: str, res_time: int, req_seq: int, channel: str) -> str:
    return hashlib.md5(f"{key}{res_time}{req_seq}{channel}".encode()).hexdigest()


def _bounded_int(value: object, default: int, min_value: int, max_value: int) -> int:
    if not isinstance(value, int | str):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


def _read_dispatch_data(raw: dict[str, Any], decrypt_key: str) -> dict[str, Any]:
    data = _read_dispatch_payload(raw, decrypt_key)
    if isinstance(data, dict):
        return data
    raise WopanResponseError("WoPan response DATA is not an object")


def _read_dispatch_payload(raw: dict[str, Any], decrypt_key: str) -> Any:
    if raw.get("STATUS") != "200":
        raise WopanResponseError(str(raw.get("MSG") or "WoPan service call failed"))
    rsp = raw.get("RSP")
    if not isinstance(rsp, dict):
        raise WopanResponseError("WoPan response missing RSP")
    code = str(rsp.get("RSP_CODE") or "")
    desc = str(rsp.get("RSP_DESC") or "")
    if code == "1001":
        raise WopanAuthenticationError(desc or "WoPan login expired")
    if code != "0000":
        raise WopanBusinessError(code, desc or "WoPan business error")
    data = rsp.get("DATA")
    if isinstance(data, dict | list):
        return data
    if isinstance(data, str) and data:
        try:
            decoded = json.loads(_decrypt_data(data, decrypt_key))
        except (
            binascii.Error,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            WopanResponseError,
        ) as exc:
            raise WopanResponseError("WoPan encrypted DATA cannot be decoded") from exc
        if isinstance(decoded, dict | list):
            return decoded
    if data == "":
        return {}
    raise WopanResponseError("WoPan response DATA cannot be decoded")


def _extract_token_from_cookie_header(cookie_header: str) -> str:
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    morsel = cookie.get(TOKEN_COOKIE_NAME)
    if morsel is None:
        raise WopanAuthenticationError(f"{TOKEN_COOKIE_NAME} not found")
    token = unquote(morsel.value).strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    if not token:
        raise WopanAuthenticationError(f"{TOKEN_COOKIE_NAME} is empty")
    return token


def _wohome_crypto_key(token: str) -> str:
    key = token[:16]
    if len(key) != 16:
        raise WopanAuthenticationError("WoPan token is too short for wohome encryption")
    return key


def _read_wopan_item(raw: dict[str, Any], fallback_parent_id: str) -> WopanItem:
    item_id = str(raw.get("id") or "")
    name = str(raw.get("name") or "")
    if not item_id:
        raise WopanResponseError("QueryAllFiles item missing id")
    if not name:
        raise WopanResponseError("QueryAllFiles item missing name")
    raw_type = _read_wopan_item_type(raw)
    if raw_type == "0":
        kind = WopanItemKind.FOLDER
    elif raw_type == "1":
        kind = WopanItemKind.FILE
    else:
        raise WopanResponseError(f"QueryAllFiles item has unknown type: {raw_type}")

    parent_id_value = raw.get("parentDirectoryId")
    return WopanItem(
        item_id=item_id,
        name=name,
        kind=kind,
        parent_id=str(parent_id_value) if parent_id_value not in (None, "") else fallback_parent_id,
        file_type=_read_optional_text(raw.get("fileType")),
        download_id=_read_optional_text(raw.get("fid")),
        size=_read_optional_int(raw.get("size")),
        updated_at=_read_wopan_timestamp(raw),
    )


def _read_wopan_item_type(raw: dict[str, Any]) -> str:
    value = raw.get("type")
    if value is None:
        return ""
    return str(value)


def _log_skipped_unknown_type_item(
    raw: dict[str, Any],
    *,
    parent_id: str,
    field_name: str,
) -> None:
    name = str(raw.get("name") or "")
    LOGGER.warning(
        "wopan.list_files.skip_item_unknown_type parent_id=%s field=%s item_id=%s "
        "raw_type=%s name_present=%s name_length=%s",
        parent_id,
        field_name,
        str(raw.get("id") or ""),
        _read_wopan_item_type(raw),
        bool(name),
        len(name),
    )


def _read_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WopanResponseError("QueryAllFiles item size is not an integer") from exc
    if parsed < 0:
        raise WopanResponseError("QueryAllFiles item size is negative")
    return parsed


def _read_required_non_negative_int(value: Any, field_name: str) -> int:
    parsed = _read_required_int(value, field_name)
    if parsed < 0:
        raise WopanResponseError(f"{field_name} is negative")
    return parsed


def _read_required_positive_int(value: Any, field_name: str) -> int:
    parsed = _read_required_int(value, field_name)
    if parsed <= 0:
        raise WopanResponseError(f"{field_name} must be positive")
    return parsed


def _read_required_int(value: Any, field_name: str) -> int:
    if value in (None, ""):
        raise WopanResponseError(f"{field_name} is missing")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WopanResponseError(f"{field_name} is not an integer") from exc


def _read_optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _wopan_kind_value(kind: WopanItemKind) -> int:
    if kind is WopanItemKind.FOLDER:
        return 0
    return 1


def _guess_upload_file_type(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}:
        return "1"
    if suffix in {"mp4", "mov", "avi", "mkv", "flv", "wmv"}:
        return "2"
    if suffix in {"mp3", "wav", "flac", "aac", "m4a"}:
        return "3"
    if suffix in {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt", "md"}:
        return "4"
    return "0"


def _read_wopan_timestamp(raw: dict[str, Any]) -> datetime | None:
    for field_name in ("updateTime", "modifyTime", "createTime"):
        value = raw.get(field_name)
        if value in (None, ""):
            continue
        value_text = str(value)
        try:
            return datetime.strptime(value_text, "%Y%m%d%H%M%S")
        except ValueError as exc:
            raise WopanResponseError(f"QueryAllFiles item {field_name} is invalid") from exc
    return None


def _encrypt_param(param: dict[str, Any], key: str) -> str:
    encoded = json.dumps(param, separators=(",", ":"), ensure_ascii=False).encode()
    cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(IV.encode()))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(_pkcs7_pad(encoded, 16)) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _decrypt_data(data: str, key: str) -> str:
    cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(IV.encode()))
    decryptor = cipher.decryptor()
    encrypted = base64.b64decode(data)
    decrypted = decryptor.update(encrypted) + decryptor.finalize()
    return _pkcs7_unpad(decrypted).decode()


def _pkcs7_pad(data: bytes, block_size: int) -> bytes:
    padding = block_size - len(data) % block_size
    return data + bytes([padding]) * padding


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WopanResponseError("invalid WoPan response padding")
    padding = data[-1]
    if padding < 1 or padding > 16:
        raise WopanResponseError("invalid WoPan response padding")
    if data[-padding:] != bytes([padding]) * padding:
        raise WopanResponseError("invalid WoPan response padding")
    return data[:-padding]
