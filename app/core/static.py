"""Static files with HTTP caching.

Pages link their assets through ``static_url`` (app/core/templating.py): ``/static/<path>?v=<version>``, the version
being the first 10 hex digits of the file's SHA-1 (``content_version``). Such a URL always means the same bytes, so a
request whose ``v`` matches the file it gets is served with ``Cache-Control: public, max-age=31536000, immutable`` -
browsers keep it for a year without asking again. Every other static URL - no ``v``, or a ``v`` that is not the current
file's (an old page after a deploy, or a new page reaching an old app copy during a rolling deploy: caching those
for a year would pin the wrong bytes to the URL) - gets ``Cache-Control: no-cache``: stored, but revalidated every time
with the ETag / Last-Modified that Starlette sends (304 when unchanged).

Only the caching header changes: Starlette's StaticFiles still serves the file (Range, HEAD, ETag, 304), and the
security headers (CSP, nosniff, ...) are added by app/security/talisman.py as for every response. No compression is
done here; a reverse proxy may compress (``no-transform`` is not set).
"""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from urllib.parse import parse_qsl

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
VERSION_LENGTH = 10
MAX_VERSIONS = 1024  # (path, mtime, size) -> version entries remembered; least recently used dropped first

_versions: OrderedDict[tuple[str, int, int], str] = OrderedDict()


def content_version(path: str | os.PathLike[str], stat_result: os.stat_result | None = None) -> str:
    """The version ``static_url`` puts in an asset's URL: the first VERSION_LENGTH hex digits of the SHA-1 of its
    bytes. Remembered per (path, modification time, size), so a rebuilt file gets a new version at once and an
    unchanged one is hashed once. Raises OSError when the file cannot be read."""
    st = stat_result if stat_result is not None else os.stat(path)
    key = (os.fspath(path), st.st_mtime_ns, st.st_size)
    version = _versions.get(key)
    if version is None:
        with open(path, "rb") as fh:
            version = hashlib.sha1(fh.read(), usedforsecurity=False).hexdigest()[:VERSION_LENGTH]
        _versions[key] = version
        if len(_versions) > MAX_VERSIONS:
            _versions.popitem(last=False)
    else:
        _versions.move_to_end(key)
    return version


def _requested_version(scope: Scope) -> str | None:
    raw = (scope.get("query_string") or b"").decode("latin-1")
    for name, value in parse_qsl(raw, keep_blank_values=False):
        if name == "v":
            return value
    return None


def cache_control(full_path: str | os.PathLike[str], stat_result: os.stat_result, scope: Scope) -> str:
    """IMMUTABLE when the request names the file's current version, else REVALIDATE."""
    wanted = _requested_version(scope)
    if wanted:
        try:
            if wanted == content_version(full_path, stat_result):
                return IMMUTABLE
        except OSError:
            pass
    return REVALIDATE


class CachedStaticFiles(StaticFiles):
    """``StaticFiles`` that sends ``cache_control`` on every file response, the 304 included."""

    def file_response(self, full_path: str | os.PathLike[str], stat_result: os.stat_result, scope: Scope,
                      status_code: int = 200) -> Response:
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        response.headers["cache-control"] = cache_control(full_path, stat_result, scope)
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response
