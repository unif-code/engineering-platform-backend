from urllib.parse import urlsplit

from fastapi import Request
from starlette.exceptions import HTTPException

_DEFAULT_PORTS = {"http": 80, "https": 443}
_FORBIDDEN_DETAIL = "Cross-origin request forbidden"


def _forbidden() -> HTTPException:
    return HTTPException(status_code=403, detail=_FORBIDDEN_DETAIL)


def _normalized_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    if (
        scheme not in _DEFAULT_PORTS
        or hostname is None
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return scheme, hostname.casefold(), port


def assert_same_origin(request: Request) -> None:
    """Enforce browser same-origin metadata.

    Requests carrying neither Origin nor Sec-Fetch-Site are treated as non-browser/server
    clients and continue to the normal authentication and authorization checks.
    """
    origins = request.headers.getlist("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if not origins and fetch_site is None:
        return

    if len(origins) > 1:
        raise _forbidden()
    if origins:
        request_hostname = request.url.hostname
        if request_hostname is None:
            raise _forbidden()
        request_origin = (
            request.url.scheme.casefold(),
            request_hostname.casefold(),
            request.url.port or _DEFAULT_PORTS.get(request.url.scheme.casefold()),
        )
        supplied_origin = _normalized_origin(origins[0])
        if supplied_origin is None or supplied_origin != request_origin:
            raise _forbidden()
    if fetch_site is not None and fetch_site != "same-origin":
        raise _forbidden()
