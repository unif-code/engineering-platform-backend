"""B0 observations through real HTTP/owner/JSONB; a passing probe does not prove hash equality."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
import control_plane.app.modules.configuration.application.drafts as draft_commands
from control_plane.app.modules.configuration.application.drafts import _content_hash
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database

pytestmark = pytest.mark.integration
_BASELINE = "43f34953a3566e491ae64ac3edb8fe791356c170"


@dataclass(repr=False)
class Probe:
    # Keep session clients out of failure argument repr; no fixture credentials are retained here.
    actor_a_id: str
    actor_b_id: str
    client_a: TestClient
    client_b: TestClient
    hash_inputs: list[dict[str, Any]]


def canonical(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() == _content_hash(value)
    return encoded


def types(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: types(item) for key, item in value.items()}
    if isinstance(value, list):
        return [types(item) for item in value]
    return type(value).__name__


@pytest.fixture
def probe(actors: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Probe]:
    observed: list[dict[str, Any]] = []

    def observe_hash(content: dict[str, Any]) -> str:
        digest = _content_hash(content)
        observed.append(
            {"canonicalJson": canonical(content), "sha256": digest, "types": types(content)}
        )
        return digest

    # Observe the production hash input without changing its arguments or result.
    monkeypatch.setattr(draft_commands, "_content_hash", observe_hash)
    with ExitStack() as stack:
        clients = [
            stack.enter_context(
                TestClient(
                    bootstrap.create_app(),
                    base_url="https://testserver",
                    raise_server_exceptions=False,
                )
            )
            for _ in range(2)
        ]
        clients[0].cookies.update(actors.a.client.cookies)
        clients[1].cookies.update(actors.b.client.cookies)
        yield Probe(actors.a.id, actors.b.id, clients[0], clients[1], observed)


def snapshot(probe: Probe, namespace: str, draft_id: str, key: str) -> dict[str, Any]:
    table = "identity.draft" if namespace == "identity" else "requirement.gate_policy_draft"
    runtime = bootstrap.configuration_http_runtime().owners.resolve(namespace)
    with runtime.transaction() as lifecycle:
        draft = lifecycle.owner.draft(draft_id)
        assert draft is not None, "Created synthetic Draft must be visible through its actual owner"
        row = (
            lifecycle.db.execute(
                text(
                    f"SELECT content::text AS jsonb_text,content_hash,revision FROM {table} "
                    "WHERE id=:id"
                ),
                {"id": draft_id},
            )
            .mappings()
            .one()
        )
        assert draft.revision == row["revision"] and draft.content_hash == row["content_hash"]
        proposed = canonical(draft.content)
        proposed_hash = _content_hash(draft.content)
        return {
            "revision": draft.revision,
            "ownerLabel": "A"
            if draft.owner_id == probe.actor_a_id
            else "B"
            if draft.owner_id == probe.actor_b_id
            else "OTHER",
            "jsonbText": row["jsonb_text"],
            "storedHash": row["content_hash"],
            "ownerValueTypes": types(draft.content),
            "probedValueRepr": repr(draft.content[key]),
            "probedValueType": type(draft.content[key]).__name__,
            "proposedContentJson": proposed,
            "proposedSha256": proposed_hash,
            "proposedTextMatchesStoredHash": proposed_hash == row["content_hash"],
            "dialect": lifecycle.db.engine.dialect.name,
            "driver": lifecycle.db.engine.dialect.driver,
        }


def request(
    probe: Probe,
    client: TestClient,
    namespace: str,
    path: str,
    body: dict[str, Any],
    *,
    key: str,
    draft_id: str | None = None,
    revision: int | None = None,
    method: str = "POST",
) -> tuple[dict[str, Any], str | None]:
    wire = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Origin": "https://testserver",
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid4()),
    }
    if revision is not None:
        headers["If-Match"] = f'"v{revision}"'
    probe.hash_inputs.clear()
    response = client.request(method, path, content=wire.encode("utf-8"), headers=headers)
    try:
        value = response.json()
    except ValueError:
        value = {}
    result: dict[str, Any] = {
        "requestJson": wire,
        "status": response.status_code,
        "hashInputs": list(probe.hash_inputs),
        # Headers/body are deliberately allowlisted; no cookies, tokens or fixture credentials.
        "etag": response.headers.get("etag"),
    }
    if response.status_code in (200, 201):
        if draft_id is None:
            draft_id = value.get("id")
        result["responseRevision"] = value.get("revision")
        result["responseContentHash"] = value.get("contentHash")
        if isinstance(value.get("content"), dict):
            result["responseContentJson"] = canonical(value["content"])
            result["responseContentTypes"] = types(value["content"])
        if "valid" in value:
            result["validationValid"] = value["valid"]
            result["issueCodes"] = [item["code"] for item in value["issues"]]
    else:
        result["problem"] = {
            field: value[field] for field in ("status", "title", "code") if field in value
        }
    if draft_id is not None:
        result["ownerReadback"] = snapshot(probe, namespace, draft_id, key)
    return result, draft_id


def emit(capfd: pytest.CaptureFixture[str], evidence: dict[str, Any]) -> None:
    # Non-sensitive fixtures only. Text fields retain exact large integers.
    encoded = json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= 32_768, "One bounded B0 evidence line per case"
    with capfd.disabled():
        print("B0_DRAFT_REPRESENTATION " + encoded, flush=True)


def characterize(
    probe: Probe,
    capfd: pytest.CaptureFixture[str],
    namespace: str,
    cases: list[tuple[str, str, Any]],
) -> list[dict[str, Any]]:
    evidence = []
    for name, key, value in cases:
        entry: dict[str, Any] = {
            "schema": "draft-representation-b0-v1",
            "productionBaseline": _BASELINE,
            "namespace": namespace,
            "case": name,
            "probedKey": key,
            "inputValueRepr": repr(value),
            "inputValueType": type(value).__name__,
            "interpretation": (
                "Characterization only: false hash equality is reported, "
                "not normalized or asserted away."
            ),
            "stages": [],
        }
        base = f"/api/v1/admin/policies/{namespace}/drafts"
        created, draft_id = request(
            probe, probe.client_a, namespace, base, {"values": {key: value}}, key=key
        )
        entry["stages"].append({"operation": "create", **created})
        if draft_id is not None:
            path = base + "/" + draft_id
            prior = created["ownerReadback"]
            for operation, client, suffix, body, method in (
                ("validate", probe.client_a, "/validate", {}, "POST"),
                (
                    "takeover",
                    probe.client_b,
                    "/takeover",
                    {"reason": "B0 synthetic representation observation"},
                    "POST",
                ),
                ("noop-update", probe.client_b, "", {"values": {}}, "PATCH"),
            ):
                result, _ = request(
                    probe,
                    client,
                    namespace,
                    path + suffix,
                    body,
                    key=key,
                    draft_id=draft_id,
                    revision=prior["revision"],
                    method=method,
                )
                current = result["ownerReadback"]
                result["revisionDelta"] = current["revision"] - prior["revision"]
                result["storedHashChanged"] = current["storedHash"] != prior["storedHash"]
                entry["stages"].append({"operation": operation, **result})
                prior = current
        else:
            entry["laterStages"] = "NOT_APPLICABLE_NO_DRAFT_CREATED"
        emit(capfd, entry)
        evidence.append(entry)
    return evidence


def test_identity_jsonb_representation_characterization(
    probe: Probe, capfd: pytest.CaptureFixture[str]
) -> None:
    cases: list[tuple[str, str, Any]] = [
        (name, "identity.session_cap", value)
        for name, value in (
            ("integer-control", 3),
            ("integer-over-js-safe", 9_007_199_254_740_993),
            ("large-integer", 10**40 + 123_456_789),
            ("exponent-20", 1e20),
            ("exponent-21", 1e21),
            ("negative-zero", -0.0),
            ("positive-zero", 0.0),
            ("integral-float", 1.0),
            ("fractional-float", 1.5),
            ("small-exponent", 1e-7),
        )
    ] + [
        ("backoff-empty-object", "identity.login_backoff", {}),
        ("backoff-null", "identity.login_backoff", None),
        (
            "unicode-and-escapes",
            "identity.password_max_age",
            '合成🧪é/é "引号" \\反斜线\n换行\t制表',
        ),
        (
            "nested-invalid-unicode",
            "identity.login_backoff",
            {"备注": ["合成", "\\\n\t"], "a": None},
        ),
    ]
    observations = characterize(probe, capfd, "identity", cases)
    assert len(observations) == 14
    # Calibration proves the real Session/owner journey runs. It asserts no representation equality.
    assert [stage["status"] for stage in observations[0]["stages"]] == [201, 200, 200, 200]
    assert [stage["ownerReadback"]["revision"] for stage in observations[0]["stages"]] == [
        1,
        2,
        3,
        4,
    ]


def test_requirement_jsonb_representation_controls(
    probe: Probe, capfd: pytest.CaptureFixture[str]
) -> None:
    observations = characterize(
        probe,
        capfd,
        "requirement.gate",
        [
            ("integer-control", "draft_archive_after_days", 7),
            ("integral-float", "draft_archive_after_days", 1.0),
            ("exponent-20", "draft_archive_after_days", 1e20),
            ("invalid-null-set", "acceptance.additional_required_capabilities", None),
        ],
    )
    assert len(observations) == 4
    assert [stage["status"] for stage in observations[0]["stages"]] == [201, 200, 200, 200]
