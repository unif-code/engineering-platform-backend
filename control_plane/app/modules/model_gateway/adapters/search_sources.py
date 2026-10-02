"""The fixed Responses search protocol. Returned URLs are metadata, never fetch targets."""

import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import TypeAdapter, ValidationError

from control_plane.app.modules.model_gateway.domain import ProviderModelId, _non_secret
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckReason,
    CheckState,
    ProbeOutcome,
    ProbeUsage,
    SearchQueryObservation,
    SearchSourceObservation,
    SearchSourceReference,
    UsageCount,
    provider_request_id,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    MAX_RESPONSE_BYTES,
    MAX_SEARCH_CALLS,
    MAX_SEARCH_QUERIES,
    MAX_SEARCH_QUERY_LENGTH,
    MAX_SEARCH_SOURCES,
    MAX_SOURCE_URL_LENGTH,
    MAX_SOURCES_PER_CALL,
    CheckKind,
    digest,
)
from control_plane.app.shared.security import sanitize_external_reference


class SearchProtocolError(ValueError):
    def __init__(self, reason: CheckReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def search_failure(
    reason: CheckReason, consumed: int, *, state: CheckState = CheckState.FAILED
) -> ProbeOutcome:
    return ProbeOutcome(
        state=state,
        reason=reason,
        observation=SearchSourceObservation(
            kind=CheckKind.SEARCH_SOURCES,
            consumed_bytes=consumed,
            completed_search_call_count=0,
            source_signal_observed=False,
            text_observed=False,
            normal_completion_observed=False,
            local_response_closed=False,
            sources=(),
            queries=(),
        ),
    )


def _public_reference(value: object) -> str:
    try:
        if not isinstance(value, str) or not 8 <= len(value) <= MAX_SOURCE_URL_LENGTH:
            raise ValueError
        decoded = unquote(value)
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or any(
            ord(char) < 32 or ord(char) == 127 for char in decoded
        ):
            raise ValueError
        _non_secret(decoded)
        parts = urlsplit(value)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError
        if parts.port not in (None, 443):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (
                len(labels) < 2
                or len(host) > 253
                or any(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                    for label in labels
                )
            ):
                raise ValueError from None
            if labels[-1] in {"localhost", "local", "internal", "lan", "home", "invalid", "test"}:
                raise ValueError from None
            if all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in labels):
                raise ValueError from None
        else:
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise ValueError
            host = f"[{address}]" if address.version == 6 else str(address)
        # This validates a reference, not DNS resolution or page content. No network is performed.
        return sanitize_external_reference(
            urlunsplit(("https", host, parts.path or "/", parts.query, parts.fragment))
        )
    except (ValueError, UnicodeError):
        raise SearchProtocolError(CheckReason.UNSAFE_SEARCH_SOURCE) from None


def _queries(action: dict[str, object], call_id: str) -> SearchQueryObservation:
    def normalize(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_SEARCH_QUERY_LENGTH:
            raise SearchProtocolError(CheckReason.INVALID_SEARCH_RESPONSE)
        result = " ".join(value.split())
        if not result:
            raise SearchProtocolError(CheckReason.INVALID_SEARCH_RESPONSE)
        return result

    single = action.get("query")
    multiple = action.get("queries")
    if single is None and multiple is None:
        return SearchQueryObservation(call_id=call_id, count=None, digest=None)
    one = None if single is None else normalize(single)
    many = None
    if multiple is not None:
        if not isinstance(multiple, list):
            raise SearchProtocolError(CheckReason.INVALID_SEARCH_RESPONSE)
        if len(multiple) > MAX_SEARCH_QUERIES:
            raise SearchProtocolError(CheckReason.SEARCH_EVIDENCE_LIMIT)
        many = list(dict.fromkeys(normalize(item) for item in multiple))
    if one is not None and many is not None and many != [one]:
        raise SearchProtocolError(CheckReason.INVALID_SEARCH_RESPONSE)
    normalized = many if many is not None else [one]
    return SearchQueryObservation(call_id=call_id, count=len(normalized), digest=digest(normalized))


def _tool_count(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError
    search = value.get("web_search")
    if search is None:
        return None
    if not isinstance(search, dict):
        raise ValueError
    count = search.get("count")
    return None if count is None else TypeAdapter(UsageCount).validate_python(count)


def parse_search_response(raw: bytes, requested_model: str) -> ProbeOutcome:
    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            return search_failure(CheckReason.RESPONSE_TOO_LARGE, MAX_RESPONSE_BYTES + 1)
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("object") != "response":
            raise ValueError
        request_id = provider_request_id(value.get("id"))
        model = TypeAdapter(ProviderModelId).validate_python(value.get("model"))
        if model != requested_model:
            return search_failure(CheckReason.MODEL_IDENTITY_MISMATCH, len(raw))
        if value.get("error") is not None or value.get("status") == "failed":
            return search_failure(CheckReason.PROVIDER_ERROR, len(raw))
        if value.get("incomplete_details") is not None or value.get("status") == "incomplete":
            return search_failure(CheckReason.RESPONSE_TRUNCATED, len(raw))
        if value.get("status") in ("queued", "in_progress"):
            return search_failure(
                CheckReason.REQUEST_OUTCOME_UNKNOWN, len(raw), state=CheckState.UNKNOWN
            )
        if value.get("status") == "cancelled":
            return search_failure(CheckReason.PROVIDER_REJECTED, len(raw))
        if value.get("status") != "completed":
            raise ValueError
        output = value.get("output")
        if not isinstance(output, list):
            raise ValueError
        if len(output) > 32:
            raise SearchProtocolError(CheckReason.SEARCH_EVIDENCE_LIMIT)
        ids: set[str] = set()
        sources: dict[tuple[str, str], SearchSourceReference] = {}
        queries: list[SearchQueryObservation] = []
        source_count = 0
        text_observed = False
        for item in output:
            if not isinstance(item, dict):
                raise ValueError
            item_id = provider_request_id(item.get("id"))
            if item_id in ids:
                raise ValueError
            ids.add(item_id)
            kind = item.get("type")
            if kind == "reasoning":
                continue  # Never persist or return the Provider's reasoning body.
            if kind == "web_search_call":
                if item.get("status") == "failed":
                    return search_failure(CheckReason.PROVIDER_ERROR, len(raw))
                if item.get("status") != "completed":
                    raise ValueError
                if len(queries) >= MAX_SEARCH_CALLS:
                    raise SearchProtocolError(CheckReason.SEARCH_EVIDENCE_LIMIT)
                action = item.get("action")
                if not isinstance(action, dict) or action.get("type") != "search":
                    raise ValueError
                queries.append(_queries(action, item_id))
                entries = action.get("sources")
                if entries is None:
                    entries = []
                if not isinstance(entries, list):
                    raise ValueError
                source_count += len(entries)
                if len(entries) > MAX_SOURCES_PER_CALL or source_count > MAX_SEARCH_SOURCES:
                    raise SearchProtocolError(CheckReason.SEARCH_EVIDENCE_LIMIT)
                for source in entries:
                    if not isinstance(source, dict) or source.get("type") != "url":
                        raise ValueError
                    url = _public_reference(source.get("url"))
                    sources.setdefault(
                        (item_id, url), SearchSourceReference(call_id=item_id, sanitized_url=url)
                    )
            elif kind == "message":
                if (
                    item.get("role") != "assistant"
                    or item.get("status") != "completed"
                    or not isinstance(item.get("content"), list)
                ):
                    raise ValueError
                if len(item["content"]) > 32:
                    raise SearchProtocolError(CheckReason.SEARCH_EVIDENCE_LIMIT)
                for part in item["content"]:
                    if not isinstance(part, dict):
                        raise ValueError
                    if part.get("type") == "refusal":
                        return search_failure(CheckReason.RESPONSE_REFUSED, len(raw))
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise ValueError
                    text_observed |= bool(part["text"].strip())
            else:
                raise SearchProtocolError(CheckReason.UNEXPECTED_TOOL_OUTPUT)
        if not text_observed:
            raise ValueError
        raw_usage = value.get("usage")
        usage = None
        count = _tool_count(value.get("x_tools"))
        if raw_usage is not None:
            if not isinstance(raw_usage, dict):
                raise ValueError
            usage = ProbeUsage.model_validate(
                {
                    "prompt_tokens": raw_usage.get("input_tokens"),
                    "completion_tokens": raw_usage.get("output_tokens"),
                    "total_tokens": raw_usage.get("total_tokens"),
                }
            )
            nested_count = _tool_count(raw_usage.get("x_tools"))
            if count is not None and nested_count is not None and count != nested_count:
                raise ValueError
            count = nested_count if nested_count is not None else count
        success = bool(queries and sources)
        return ProbeOutcome(
            state=CheckState.SUCCEEDED if success else CheckState.FAILED,
            reason=None if success else CheckReason.SEARCH_SOURCE_SIGNAL_MISSING,
            provider_request_id=request_id,
            reported_model_id=model,
            usage=usage,
            observation=SearchSourceObservation(
                kind=CheckKind.SEARCH_SOURCES,
                consumed_bytes=len(raw),
                completed_search_call_count=len(queries),
                source_signal_observed=success,
                provider_search_call_count=count,
                text_observed=text_observed,
                normal_completion_observed=True,
                local_response_closed=False,
                sources=tuple(sources.values()),
                queries=tuple(queries),
            ),
        )
    except SearchProtocolError as error:
        return search_failure(error.reason, min(len(raw), MAX_RESPONSE_BYTES + 1))
    except (ValueError, TypeError, ValidationError, UnicodeError, RecursionError):
        return search_failure(
            CheckReason.INVALID_SEARCH_RESPONSE, min(len(raw), MAX_RESPONSE_BYTES + 1)
        )
