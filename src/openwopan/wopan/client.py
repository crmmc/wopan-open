from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import mimetypes
import time
from collections.abc import Callable, Iterable, Sequence
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
    WopanUploadCancelledError,
)
from openwopan.wopan.models import (
    DownloadInfo,
    WopanCloudUsage,
    WopanItem,
    WopanItemKind,
    WopanRecycleItem,
)

UploadProgressCallback = Callable[[int, int], None]
UploadPartResultCallback = Callable[[int, str], None]


@dataclass(frozen=True, slots=True)
class UploadResumeContext:
    """Reuses one upload session to skip already-confirmed parts.

    ``on_part_result`` is invoked from the client's worker threads after each
    part is confirmed with ``code == "0000"``; ``fid`` carries that response's
    ``data.fid`` or an empty string when the response has none.
    """

    unique_id: str
    batch_no: str
    completed_indexes: frozenset[int] = frozenset()
    known_fid: str = ""
    on_part_result: UploadPartResultCallback | None = None


def _check_upload_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise WopanUploadCancelledError("上传已取消")


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
PERSONAL_SEARCH_TYPE = "2"
SEARCH_DEFAULT_PAGE_SIZE = 50
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

    def search_files(
        self, keyword: str, page_no: int = 1, page_size: int = SEARCH_DEFAULT_PAGE_SIZE
    ) -> list[WopanItem]:
        """Search personal-space files by keyword across all directories."""
        if not keyword.strip():
            raise ValueError("keyword must not be empty")
        if page_no < 1:
            raise ValueError("page_no must be at least 1")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        LOGGER.info(
            "wopan.search_files.start keyword_length=%s page_no=%s page_size=%s",
            len(keyword),
            page_no,
            page_size,
        )
        data = self._dispatch_wohome(
            "SearchFile",
            {
                "searchType": PERSONAL_SEARCH_TYPE,
                "keyWord": keyword,
                "pageNo": page_no,
                "pageSize": page_size,
                "clientId": CLIENT_ID,
            },
        )
        raw_items = data.get("personalResult") or []
        if not isinstance(raw_items, list):
            raise WopanResponseError("SearchFile personalResult is not a list")
        items: list[WopanItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise WopanResponseError("SearchFile item is not an object")
            items.append(_read_search_item(raw_item))
        LOGGER.info(
            "wopan.search_files.success keyword_length=%s item_count=%s",
            len(keyword),
            len(items),
        )
        return items

    def get_directory_path(self, directory_id: str) -> list[tuple[str, str]]:
        """Resolve a directory id to its root-relative id/name path in one call."""
        if not directory_id:
            raise ValueError("directory_id must not be empty")

        LOGGER.info("wopan.get_directory_path.start directory_id=%s", directory_id)
        data = self._dispatch_wohome_payload(
            "GetDirectoryPath",
            {"directoryId": directory_id, "clientId": CLIENT_ID},
        )
        if not isinstance(data, list):
            raise WopanResponseError("GetDirectoryPath DATA is not a list")
        chain: list[tuple[str, str]] = []
        for raw_item in reversed(data):
            if not isinstance(raw_item, dict):
                raise WopanResponseError("GetDirectoryPath item is not an object")
            item_id = str(raw_item.get("id") or "")
            name = str(raw_item.get("directoryName") or "")
            if not item_id or not name:
                raise WopanResponseError("GetDirectoryPath item missing id or name")
            chain.append((item_id, name))
        LOGGER.info(
            "wopan.get_directory_path.success directory_id=%s depth=%s",
            directory_id,
            len(chain),
        )
        return chain

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
        self.delete_many([(item_id, kind)])

    def delete_many(self, items: Sequence[tuple[str, WopanItemKind]]) -> None:
        """Delete one or more files and folders in a single request."""
        if not items:
            raise ValueError("items must not be empty")
        for item_id, _kind in items:
            if not item_id:
                raise ValueError("item_id must not be empty")

        dir_ids = [item_id for item_id, kind in items if kind is WopanItemKind.FOLDER]
        file_ids = [item_id for item_id, kind in items if kind is WopanItemKind.FILE]

        LOGGER.info("wopan.delete_many.start folders=%s files=%s", len(dir_ids), len(file_ids))
        self._dispatch_wohome(
            "DeleteFile",
            {
                "spaceType": PERSONAL_SPACE_TYPE,
                "vipLevel": DEFAULT_VIP_LEVEL,
                "dirList": dir_ids,
                "fileList": file_ids,
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info("wopan.delete_many.success folders=%s files=%s", len(dir_ids), len(file_ids))

    def move(self, item_id: str, kind: WopanItemKind, target_parent_id: str) -> None:
        """Move a file or folder to another parent directory."""
        self.move_many([(item_id, kind)], target_parent_id)

    def move_many(self, items: Sequence[tuple[str, WopanItemKind]], target_parent_id: str) -> None:
        """Move one or more files and folders in a single request."""
        if not target_parent_id:
            raise ValueError("target_parent_id must not be empty")
        if not items:
            raise ValueError("items must not be empty")
        for item_id, _kind in items:
            if not item_id:
                raise ValueError("item_id must not be empty")

        dir_ids = [item_id for item_id, kind in items if kind is WopanItemKind.FOLDER]
        file_ids = [item_id for item_id, kind in items if kind is WopanItemKind.FILE]

        LOGGER.info(
            "wopan.move_many.start folders=%s files=%s target_parent_id=%s",
            len(dir_ids),
            len(file_ids),
            target_parent_id,
        )
        self._dispatch_wohome(
            "MoveFile",
            {
                "targetDirId": target_parent_id,
                "sourceType": PERSONAL_SPACE_TYPE,
                "targetType": PERSONAL_SPACE_TYPE,
                "dirList": dir_ids,
                "fileList": file_ids,
                "secret": False,
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info(
            "wopan.move_many.success folders=%s files=%s target_parent_id=%s",
            len(dir_ids),
            len(file_ids),
            target_parent_id,
        )

    def copy(self, item_id: str, kind: WopanItemKind, target_parent_id: str) -> None:
        """Copy a file or folder to another parent directory."""
        self.copy_many([(item_id, kind)], target_parent_id)

    def copy_many(self, items: Sequence[tuple[str, WopanItemKind]], target_parent_id: str) -> None:
        """Copy one or more files and folders in a single request."""
        if not target_parent_id:
            raise ValueError("target_parent_id must not be empty")
        if not items:
            raise ValueError("items must not be empty")
        for item_id, _kind in items:
            if not item_id:
                raise ValueError("item_id must not be empty")

        dir_ids = [item_id for item_id, kind in items if kind is WopanItemKind.FOLDER]
        file_ids = [item_id for item_id, kind in items if kind is WopanItemKind.FILE]

        LOGGER.info(
            "wopan.copy_many.start folders=%s files=%s target_parent_id=%s",
            len(dir_ids),
            len(file_ids),
            target_parent_id,
        )
        self._dispatch_wohome(
            "CopyFile",
            {
                "targetDirId": target_parent_id,
                "sourceType": PERSONAL_SPACE_TYPE,
                "targetType": PERSONAL_SPACE_TYPE,
                "dirList": dir_ids,
                "fileList": file_ids,
                "secret": False,
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info(
            "wopan.copy_many.success folders=%s files=%s target_parent_id=%s",
            len(dir_ids),
            len(file_ids),
            target_parent_id,
        )

    def list_recycle_items(self, max_items: int = 2000) -> list[WopanRecycleItem]:
        """List recycle-bin entries, paging until the listing is exhausted."""
        if max_items < 1:
            raise ValueError("max_items must be at least 1")

        LOGGER.info("wopan.list_recycle_items.start max_items=%s", max_items)
        items: list[WopanRecycleItem] = []
        seen_delete_nos: set[str] = set()
        page_no = 1
        while True:
            data = self._dispatch_wohome_payload(
                "QueryRecycleData",
                {
                    "pageNo": page_no,
                    "pageSize": DEFAULT_PAGE_SIZE,
                    "sortRule": DEFAULT_SORT_RULE,
                    "clientId": CLIENT_ID,
                },
            )
            if not isinstance(data, list):
                raise WopanResponseError("QueryRecycleData DATA is not a list")
            page_items: list[WopanRecycleItem] = []
            for raw_item in data:
                if not isinstance(raw_item, dict):
                    raise WopanResponseError("QueryRecycleData item is not an object")
                page_items.append(_read_recycle_item(raw_item))
            new_count = 0
            for item in page_items:
                if item.delete_no in seen_delete_nos:
                    continue
                seen_delete_nos.add(item.delete_no)
                items.append(item)
                new_count += 1
                if len(items) >= max_items:
                    break
            # Stop on a short page, on a page whose deleteNos were all already
            # collected (the server may ignore pageNo and echo the same page),
            # or once max_items caps the listing.
            if len(items) >= max_items:
                break
            if len(page_items) < DEFAULT_PAGE_SIZE:
                break
            if new_count == 0:
                break
            page_no += 1
        LOGGER.info(
            "wopan.list_recycle_items.success item_count=%s page_count=%s",
            len(items),
            page_no,
        )
        return items

    def restore_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Restore one or more recycle-bin entries to their original locations."""
        if not delete_nos:
            raise ValueError("delete_nos must not be empty")
        for delete_no in delete_nos:
            if not delete_no:
                raise ValueError("delete_no must not be empty")

        LOGGER.info("wopan.restore_recycle_items.start count=%s", len(delete_nos))
        self._dispatch_wohome(
            "ReductionRecycleData",
            {
                "deleteNos": list(delete_nos),
                "deviceNo": CLIENT_ID,
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info("wopan.restore_recycle_items.success count=%s", len(delete_nos))

    def purge_recycle_items(self, delete_nos: Sequence[str]) -> None:
        """Permanently delete one or more recycle-bin entries."""
        if not delete_nos:
            raise ValueError("delete_nos must not be empty")
        for delete_no in delete_nos:
            if not delete_no:
                raise ValueError("delete_no must not be empty")

        LOGGER.info("wopan.purge_recycle_items.start count=%s", len(delete_nos))
        self._dispatch_wohome(
            "DeleteRecycleData",
            {
                "deleteNos": list(delete_nos),
                "clientId": CLIENT_ID,
            },
        )
        LOGGER.info("wopan.purge_recycle_items.success count=%s", len(delete_nos))

    def empty_recycle_bin(self) -> None:
        """Permanently delete every recycle-bin entry."""
        LOGGER.info("wopan.empty_recycle_bin.start")
        self._dispatch_wohome(
            "EmptyRecycleData",
            {"clientId": CLIENT_ID},
        )
        LOGGER.info("wopan.empty_recycle_bin.success")

    def upload_file(
        self,
        parent_id: str,
        local_path: Path,
        *,
        upload_part_size_mb: int = 5,
        max_upload_threads: int = 16,
        retry_max_attempts: int = 3,
        upload_name: str | None = None,
        progress_callback: UploadProgressCallback | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        resume: UploadResumeContext | None = None,
    ) -> WopanItem:
        """Upload a local file to a parent directory.

        With ``resume`` the previous session's ``uniqueId``/``batchNo`` are
        reused, already-confirmed parts are skipped, and the request form
        stays byte-identical to the original session so the server can
        aggregate parts under the same uniqueId. When a resumable upload
        finishes its parts without a usable fid, or fails after the server
        may already have assembled the file, the target directory listing is
        queried to recover the fid by name+size match.
        """
        if not parent_id:
            raise ValueError("parent_id must not be empty")
        if not local_path.is_file():
            raise ValueError("local_path must be an existing file")

        _check_upload_cancelled(cancel_requested)
        file_size = local_path.stat().st_size
        file_name = upload_name if upload_name is not None else local_path.name
        part_size, total_parts = resolve_upload_part_plan(file_size, upload_part_size_mb)
        max_workers = min(_bounded_int(max_upload_threads, 16, 1, 16), total_parts)
        max_attempts = _bounded_int(retry_max_attempts, 3, 0, 5) + 1
        completed_indexes = _valid_completed_indexes(resume, total_parts)
        upload_file_type = guess_upload_file_type(file_name)

        if (
            resume is not None
            and resume.known_fid
            and completed_indexes == set(range(1, total_parts + 1))
        ):
            # Defensive: the service layer short-circuits earlier; never resend
            # a fully uploaded file just to re-derive a known fid.
            if progress_callback is not None:
                progress_callback(file_size, file_size)
            return build_uploaded_file_item(
                file_name=file_name,
                parent_id=parent_id,
                file_size=file_size,
                fid=resume.known_fid,
            )

        if not pending_upload_indexes(total_parts, completed_indexes):
            # Every part is already confirmed but no fid was captured. UAT
            # (2026-09-26): resending an already-assembled part returns an
            # empty body, so the fid cannot be recovered from the part
            # response; the server has assembled the file, so it must exist
            # in the target directory. Recover the fid from the listing (R7).
            _check_upload_cancelled(cancel_requested)
            recovered = self._recover_upload_item_from_listing(
                parent_id=parent_id, file_name=file_name, file_size=file_size
            )
            if recovered is None:
                LOGGER.warning(
                    "wopan.upload_file.recovery_missed parent_id=%s "
                    "file_name_length=%s file_size=%s",
                    parent_id,
                    len(file_name),
                    file_size,
                )
                raise WopanResponseError("上传已完成但目标目录未找到对应文件，请刷新后重试")
            if progress_callback is not None:
                progress_callback(file_size, file_size)
            return recovered

        if cancel_requested is None:
            zone_url = self.get_upload_zone_url(retry_max_attempts=retry_max_attempts)
        else:
            zone_url = self.get_upload_zone_url(
                retry_max_attempts=retry_max_attempts, cancel_requested=cancel_requested
            )
        _check_upload_cancelled(cancel_requested)
        upload_url = f"{zone_url.rstrip('/')}/openapi/client/upload2C"
        unique_id = resume.unique_id if resume is not None else str(int(time.time() * 1000))
        batch_no = resume.batch_no if resume is not None else time.strftime("%Y%m%d%H%M%S")
        token_key = _wohome_crypto_key(self._access_token)
        file_info = {
            "spaceType": PERSONAL_SPACE_TYPE,
            "directoryId": parent_id,
            "batchNo": batch_no,
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
        on_part_result = resume.on_part_result if resume is not None else None

        LOGGER.info(
            "wopan.upload_file.start parent_id=%s file_name_length=%s file_size=%s "
            "total_parts=%s workers=%s completed_parts=%s",
            parent_id,
            len(file_name),
            file_size,
            total_parts,
            max_workers,
            len(completed_indexes),
        )
        try:
            # Single-part plans also run through the multipart executor: one
            # part is just a one-element executor task, so request fields,
            # retry, cancel, resume-skip and fid handling stay identical.
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
                progress_callback=progress_callback,
                cancel_requested=cancel_requested,
                completed_indexes=completed_indexes,
                on_part_result=on_part_result,
            )

            _check_upload_cancelled(cancel_requested)
            code = str(raw.get("code") or "")
            if code != "0000":  # pragma: no cover - docs/testing-exemptions.md
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
            return build_uploaded_file_item(
                file_name=file_name,
                parent_id=parent_id,
                file_size=file_size,
                fid=fid,
            )
        except httpx.HTTPError:
            LOGGER.warning("wopan.upload_file.http_error parent_id=%s", parent_id)
            raise
        except (OSError, ValueError) as exc:
            LOGGER.warning("wopan.upload_file.response_error parent_id=%s", parent_id)
            raise WopanResponseError("upload2C response cannot be decoded") from exc
        except WopanUploadCancelledError:
            raise
        except Exception as exc:
            # Resumable uploads only: a part or response failure after the
            # server already assembled the file can still mean success, so
            # check the target directory listing before giving up. Fresh
            # uploads (resume is None) never spend an extra request here.
            if resume is None:
                raise
            recovered = self._recover_upload_item_on_error(
                original_error=exc,
                parent_id=parent_id,
                file_name=file_name,
                file_size=file_size,
            )
            if recovered is None:
                raise
            if progress_callback is not None:
                progress_callback(file_size, file_size)
            return recovered

    def _recover_upload_item_from_listing(
        self, *, parent_id: str, file_name: str, file_size: int
    ) -> WopanItem | None:
        """Find the finished upload in its target directory by name and size.

        UAT (2026-09-26): once the server has assembled all parts the file
        exists in the cloud even when the client never captured the fid from
        the final part response. Only a FILE entry matching both the exact
        name and the exact size counts, so an older same-name file is never
        mistaken for this upload; same-name conflicts are already rejected by
        the service layer's pre-upload check, so the first hit is taken.
        Entries without a fid cannot yield a usable item and are ignored.
        """
        for item in self.list_files(parent_id):
            if (
                item.kind is WopanItemKind.FILE
                and item.name == file_name
                and item.size == file_size
                and item.download_id
            ):
                return build_uploaded_file_item(
                    file_name=file_name,
                    parent_id=parent_id,
                    file_size=file_size,
                    fid=item.download_id,
                )
        return None

    def _recover_upload_item_on_error(
        self,
        *,
        original_error: Exception,
        parent_id: str,
        file_name: str,
        file_size: int,
    ) -> WopanItem | None:
        """Best-effort listing recovery before re-raising a failed resumable upload.

        A listing failure (network/decryption) must never mask the original
        upload error: it is logged and ``None`` is returned so the caller can
        re-raise the original exception.
        """
        try:
            recovered = self._recover_upload_item_from_listing(
                parent_id=parent_id, file_name=file_name, file_size=file_size
            )
        except Exception as recovery_error:
            LOGGER.warning(
                "wopan.upload_file.listing_recovery_failed parent_id=%s "
                "original_error_type=%s recovery_error_type=%s",
                parent_id,
                type(original_error).__name__,
                type(recovery_error).__name__,
            )
            return None
        if recovered is not None:
            LOGGER.info(
                "wopan.upload_file.recovered_from_listing parent_id=%s original_error_type=%s",
                parent_id,
                type(original_error).__name__,
            )
        return recovered

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
        progress_callback: UploadProgressCallback | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        completed_indexes: set[int] | frozenset[int] = frozenset(),
        on_part_result: UploadPartResultCallback | None = None,
    ) -> dict[str, Any]:
        if total_parts <= 0:
            raise WopanResponseError("upload2C multipart upload produced no response")
        last_raw: dict[str, Any] | None = None
        first_fid_raw: dict[str, Any] | None = None
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures: dict[Any, int] = {}
            file_size = local_path.stat().st_size
            pending = pending_upload_indexes(total_parts, completed_indexes)
            completed_bytes = sum(
                min(part_size, max(0, file_size - (index - 1) * part_size))
                for index in completed_indexes
            )
            for part_index in pending:
                _check_upload_cancelled(cancel_requested)
                offset = (part_index - 1) * part_size
                part_length = min(part_size, max(0, file_size - offset))
                future = executor.submit(
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
                    cancel_requested,
                    on_part_result,
                )
                futures[future] = part_length
            for future in as_completed(futures):
                _check_upload_cancelled(cancel_requested)
                try:
                    raw = future.result()
                except Exception:
                    _check_upload_cancelled(cancel_requested)
                    raise
                _check_upload_cancelled(cancel_requested)
                completed_bytes += futures[future]
                if progress_callback is not None:
                    progress_callback(completed_bytes, file_size)
                last_raw = raw
                if first_fid_raw is None and _extract_part_fid(raw):
                    first_fid_raw = raw
        if last_raw is None:
            raise WopanResponseError("upload2C multipart upload produced no response")
        return first_fid_raw if first_fid_raw is not None else last_raw

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
        cancel_requested: Callable[[], bool] | None = None,
        on_part_result: UploadPartResultCallback | None = None,
    ) -> dict[str, Any]:
        _check_upload_cancelled(cancel_requested)
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
            cancel_requested,
            on_part_result,
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
        cancel_requested: Callable[[], bool] | None = None,
        on_part_result: UploadPartResultCallback | None = None,
    ) -> dict[str, Any]:
        form_data = {
            **base_form_data,
            "partSize": str(len(content)),
            "partIndex": str(part_index),
        }
        last_error: Exception | None = None
        for _attempt in range(max_attempts):
            _check_upload_cancelled(cancel_requested)
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
                _check_upload_cancelled(cancel_requested)
                response.raise_for_status()
                raw = response.json()
                if not isinstance(raw, dict):
                    raise WopanResponseError("upload2C response is not an object")
                code = str(raw.get("code") or "")
                if code != "0000":
                    message = str(raw.get("msg") or "WoPan upload failed")
                    raise WopanBusinessError(code, message)
                if on_part_result is not None:
                    on_part_result(part_index, _extract_part_fid(raw))
                return raw
            except (httpx.HTTPError, WopanBusinessError) as exc:
                _check_upload_cancelled(cancel_requested)
                last_error = exc
            except ValueError as exc:
                raise WopanResponseError("upload2C response cannot be decoded") from exc
        if last_error is not None:
            raise last_error
        raise WopanResponseError("upload2C upload part failed")

    def get_upload_zone_url(
        self, *, retry_max_attempts: int = 3, cancel_requested: Callable[[], bool] | None = None
    ) -> str:
        """Return the current upload zone URL, retrying transient gateway errors."""
        LOGGER.debug("wopan.get_upload_zone_url.start")
        attempts = _bounded_int(retry_max_attempts, 3, 0, 5) + 1
        for attempt in range(attempts):
            _check_upload_cancelled(cancel_requested)
            try:
                data = self._dispatch_wohome(
                    "GetZoneInfo",
                    {"appId": DEFAULT_UPLOAD_APP_ID},
                    body_extra={"key": True},
                )
                _check_upload_cancelled(cancel_requested)
                break
            except httpx.HTTPStatusError as exc:
                _check_upload_cancelled(cancel_requested)
                if exc.response.status_code not in {502, 503, 504} or attempt + 1 >= attempts:
                    raise
                LOGGER.warning(
                    "wopan.get_upload_zone_url.transient_http_error status=%s attempt=%s",
                    exc.response.status_code,
                    attempt + 1,
                )
            except httpx.HTTPError:
                _check_upload_cancelled(cancel_requested)
                raise
        else:  # pragma: no cover - loop always returns or raises
            raise WopanResponseError("上传节点查询失败")
        _check_upload_cancelled(cancel_requested)
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


def resolve_upload_part_plan(file_size: int, upload_part_size_mb: int) -> tuple[int, int]:
    """Return the single source of truth for ``(part_size, total_parts)``."""
    part_size = _bounded_int(upload_part_size_mb, 5, 5, 16) * BYTES_PER_MB
    total_parts = max(1, (file_size + part_size - 1) // part_size)
    return part_size, total_parts


def pending_upload_indexes(total_parts: int, completed_indexes: Iterable[int] | None) -> list[int]:
    """Return 1-based part indexes that still need an upload request."""
    completed = set(completed_indexes or ())
    return [index for index in range(1, total_parts + 1) if index not in completed]


def _valid_completed_indexes(resume: UploadResumeContext | None, total_parts: int) -> set[int]:
    """Filter resume part indexes down to the valid 1..total_parts range."""
    if resume is None:
        return set()
    return {index for index in resume.completed_indexes if 1 <= index <= total_parts}


def _extract_part_fid(raw: dict[str, Any]) -> str:
    data = raw.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("fid") or "")


def build_uploaded_file_item(
    *,
    file_name: str,
    parent_id: str,
    file_size: int,
    fid: str,
) -> WopanItem:
    """Build the result item for one completed upload (shared with the app layer)."""
    return WopanItem(
        item_id=fid,
        name=file_name,
        kind=WopanItemKind.FILE,
        parent_id=parent_id,
        file_type=guess_upload_file_type(file_name),
        download_id=fid,
        size=file_size,
    )


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
        sha256=_read_optional_text(raw.get("sha256")),
    )


def _read_search_item(raw: dict[str, Any]) -> WopanItem:
    item_id = str(raw.get("id") or "")
    name = str(raw.get("fileName") or raw.get("name") or "")
    if not item_id:
        raise WopanResponseError("SearchFile item missing id")
    if not name:
        raise WopanResponseError("SearchFile item missing fileName")
    # Live-verified (2026-09-29): SearchFile results carry an EMPTY `type` and
    # only ever match files, so empty/absent type maps to FILE; "0" stays a
    # folder for forward compatibility.
    raw_type = _read_wopan_item_type(raw)
    if raw_type == "0":
        kind = WopanItemKind.FOLDER
    else:
        kind = WopanItemKind.FILE
    size = _read_optional_int(raw.get("fileSize"))
    if size is None:
        size = _read_optional_int(raw.get("size"))
    parent_id_value = raw.get("directoryId")
    return WopanItem(
        item_id=item_id,
        name=name,
        kind=kind,
        parent_id=str(parent_id_value) if parent_id_value not in (None, "") else "",
        file_type=_read_optional_text(raw.get("fileType")),
        download_id=_read_optional_text(raw.get("fid")),
        size=size,
        updated_at=_read_wopan_timestamp(raw),
        sha256=_read_optional_text(raw.get("sha256")),
    )


def _read_recycle_item(raw: dict[str, Any]) -> WopanRecycleItem:
    delete_no = str(raw.get("deleteNo") or "")
    name = str(raw.get("name") or "")
    item_id = str(raw.get("id") or raw.get("fid") or "")
    if not delete_no:
        raise WopanResponseError("QueryRecycleData item missing deleteNo")
    if not item_id:
        raise WopanResponseError("QueryRecycleData item missing id")
    if not name:
        raise WopanResponseError("QueryRecycleData item missing name")
    raw_type = _read_wopan_item_type(raw)
    if raw_type == "0":
        kind = WopanItemKind.FOLDER
    elif raw_type == "1":
        kind = WopanItemKind.FILE
    else:
        raise WopanResponseError(f"QueryRecycleData item has unknown type: {raw_type}")
    try:
        deleted_at = _read_wopan_timestamp(raw, ("deleteTime",))
    except WopanResponseError:
        # `deleteTime` 的线上格式未经真机确认，解析失败按设计降级为 None（展示 "--"）。
        deleted_at = None
    return WopanRecycleItem(
        delete_no=delete_no,
        item_id=item_id,
        name=name,
        kind=kind,
        size=_read_optional_int(raw.get("fileSize"), "QueryRecycleData item fileSize"),
        deleted_at=deleted_at,
        keep_days=_read_optional_int(raw.get("keepDays"), "QueryRecycleData item keepDays"),
        file_type=_read_optional_text(raw.get("fileType")),
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


def _read_optional_int(value: Any, field_name: str = "QueryAllFiles item size") -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WopanResponseError(f"{field_name} is not an integer") from exc
    if parsed < 0:
        raise WopanResponseError(f"{field_name} is negative")
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


def guess_upload_file_type(name: str) -> str:
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


# Backwards-compatible private alias (existing tests reference the private name).
_guess_upload_file_type = guess_upload_file_type


def _read_wopan_timestamp(
    raw: dict[str, Any],
    field_names: Sequence[str] = ("updateTime", "modifyTime", "createTime"),
) -> datetime | None:
    for field_name in field_names:
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
