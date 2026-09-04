from urllib.parse import urlsplit, urlunsplit


def sanitize_external_reference(value: str) -> str:
    """Return a stable external reference without credential-like URL components."""

    normalized = value.strip()
    if not normalized:
        raise ValueError("external reference is required")
    parsed = urlsplit(normalized)
    if parsed.scheme in {"http", "https"}:
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("external URL is invalid")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if parsed.scheme == "urn" and parsed.path:
        return urlunsplit((parsed.scheme, "", parsed.path, "", ""))
    raise ValueError("external reference must use https, http, or urn")
