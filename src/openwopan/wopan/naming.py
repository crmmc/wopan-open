"""Server-side naming rules observed from the WoPan storage service."""

from __future__ import annotations

#: Names longer than this are truncated by the server before being stored.
SERVER_FILE_NAME_LIMIT = 100


def server_file_name(name: str) -> str:
    """Return the name the server stores for ``name``.

    UAT (2026-10-06, 157 uploaded ``.mkv`` files, all matching): names whose
    total length exceeds ``SERVER_FILE_NAME_LIMIT`` are stored with the stem
    cut so the full name (stem + ``.`` + extension) fits the limit exactly.
    Names at or below the limit are stored verbatim; names without an
    extension are assumed verbatim (server behavior not yet observed for
    them). Callers must keep comparing names by this stored form, never by
    the local original, or long-named files look "missing" after upload.
    The limit is assumed to count characters as Python does; all observed
    samples were ASCII, so a CJK long-name probe is still pending.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or len(name) <= SERVER_FILE_NAME_LIMIT:
        return name
    keep = SERVER_FILE_NAME_LIMIT - len(ext) - 1
    if keep <= 0:
        return name
    return f"{stem[:keep]}.{ext}"
