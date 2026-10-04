from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.configuration.adapters.identity import IdentityPolicyOwner
from control_plane.app.modules.configuration.api.routes import (
    ConfigurationHttpRuntime,
    create_configuration_router,
)
from control_plane.app.modules.configuration.application.drafts import _content_hash
from control_plane.app.modules.configuration.application.lifecycle import PolicyLifecycle
from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftNotFound,
    InvalidPolicyValue,
    PolicyKey,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.identity.domain.configuration_policy import (
    _POLICY_DEFINITIONS,
    IDENTITY_POLICY_SCHEMA_REVISION,
    validate_and_materialize_identity_policy,
)
from control_plane.app.modules.requirement.adapters.gate_policy import (
    SqlAlchemyGatePolicyRepository,
)
from control_plane.app.modules.requirement.domain.gate_policy import (
    ACCEPTANCE_KEY,
    ARCHIVE_KEY,
    FORMAL_KEY,
    GatePolicy,
)

NOW = datetime(2026, 10, 4, tzinfo=UTC)


def comparison_state(namespace: str = "identity") -> Any:
    if namespace == "identity":
        catalog = [
            PolicyKey(
                key=key,
                namespace=namespace,
                value_type=value.value_type,
                unit=value.unit,
                default_value=deepcopy(value.default_value),
                min_value=value.min_value,
                max_value=value.max_value,
                enum_values=deepcopy(value.enum_values),
                effect_semantics=value.effect_semantics,
                schema_revision=IDENTITY_POLICY_SCHEMA_REVISION,
            )
            for key, value in _POLICY_DEFINITIONS.items()
        ]
        base_values = {
            key: deepcopy(value.default_value) for key, value in _POLICY_DEFINITIONS.items()
        }
        current_values = {
            **deepcopy(base_values),
            "identity.temp_credential_ttl": 48,
            "identity.password_max_age": 90,
            "identity.session_cap": 4,
        }
        draft_values = {
            **deepcopy(base_values),
            "identity.password_max_age": 90,
            "identity.session_cap": 5,
            "identity.session_idle_timeout": 45,
        }
    else:
        catalog = SqlAlchemyGatePolicyRepository(Mock()).catalog(namespace)
        base_values = {ACCEPTANCE_KEY: [], FORMAL_KEY: [], ARCHIVE_KEY: 30}
        current_values = {ACCEPTANCE_KEY: ["code.change"], FORMAL_KEY: [], ARCHIVE_KEY: 20}
        draft_values = {
            ACCEPTANCE_KEY: ["code.change"],
            FORMAL_KEY: ["code.change"],
            ARCHIVE_KEY: 10,
        }

    def normalize(name: str, *, schema_revision: int, values: dict[str, Any]) -> dict[str, Any]:
        if name != namespace or schema_revision != 1:
            raise PolicySnapshotUnavailable("Unsupported schema")
        if namespace == "identity":
            issues, policy = validate_and_materialize_identity_policy(schema_revision, values)
            if issues or policy is None:
                raise InvalidPolicyValue("Invalid candidate")
            return deepcopy(values)
        try:
            return GatePolicy.parse(
                values, namespace=name, scope="PLATFORM", schema_revision=schema_revision
            ).values()
        except ValueError:
            raise InvalidPolicyValue("Invalid candidate") from None

    for values in (base_values, current_values, draft_values):
        normalize(namespace, schema_revision=1, values=values)
    base = PolicySnapshot(
        namespace=namespace,
        scope="PLATFORM",
        version=1,
        schema_revision=1,
        snapshot_hash=_content_hash(base_values),
        values=base_values,
    )
    current = PolicySnapshot(
        namespace=namespace,
        scope="PLATFORM",
        version=2,
        schema_revision=1,
        snapshot_hash=_content_hash(current_values),
        values=current_values,
    )
    draft = Draft(
        id="known-draft",
        namespace=namespace,
        scope="PLATFORM",
        owner_id="other-admin",
        revision=7,
        status="DRAFT",
        stale=True,
        base_version=1,
        schema_revision=1,
        content=draft_values,
        content_hash=_content_hash(draft_values),
        last_meaningful_activity_at=NOW,
        archived_at=None,
        validation_evidence={"valid": True},
        preview_evidence={"historical": True},
    )
    owner = Mock()
    owner.draft.return_value = draft
    owner.version_snapshot.return_value = base
    owner.active_snapshot.return_value = current
    owner.catalog.return_value = catalog
    owner.normalize_candidate.side_effect = normalize
    db, dependencies = Mock(), Mock()
    return SimpleNamespace(
        namespace=namespace,
        base=base,
        current=current,
        draft=draft,
        catalog=catalog,
        owner=owner,
        db=db,
        dependencies=dependencies,
        lifecycle=PolicyLifecycle(db, owner, dependencies),
    )


def compare(h: Any, **overrides: Any) -> Any:
    assert hasattr(h.lifecycle, "base_comparison"), "read-only comparison is missing"
    return h.lifecycle.base_comparison(
        **{
            "namespace": h.namespace,
            "draft_id": h.draft.id,
            "expected_revision": 7,
            **overrides,
        }
    )


def assert_read_only(h: Any) -> None:
    assert {call[0] for call in h.owner.mock_calls} <= {
        "draft",
        "version_snapshot",
        "active_snapshot",
        "catalog",
        "normalize_candidate",
    }
    assert all(call.kwargs.get("for_update") is not True for call in h.owner.mock_calls)
    assert h.db.mock_calls == [] and h.dependencies.mock_calls == []


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("archived", [False, True])
def test_comparison_uses_exact_base_current_and_complete_real_owner_values_without_writes(
    namespace: str,
    archived: bool,
) -> None:
    h = comparison_state(namespace)
    if archived:
        h.owner.draft.return_value = h.draft.model_copy(
            update={"status": "ARCHIVED", "archived_at": NOW}
        )
    before = deepcopy((h.owner.draft.return_value, h.base, h.current, h.catalog))
    result = compare(h)
    assert result.draft_id == h.draft.id and result.owner_id == "other-admin"
    assert result.draft_revision == 7 and result.base_version == 1 and result.current_version == 2
    assert result.base_snapshot_hash == h.base.snapshot_hash
    assert result.current_snapshot_hash == h.current.snapshot_hash
    assert result.draft_content_hash == h.draft.content_hash
    assert result.schema_revision == 1 and result.scope == "PLATFORM"
    assert result.status == ("ARCHIVED" if archived else "DRAFT")
    assert [item.key for item in result.items] == sorted(item.key for item in h.catalog)
    changes = {item.key: item.change for item in result.items}
    if namespace == "identity":
        assert set(changes.values()) == {
            "UNCHANGED",
            "CURRENT_ONLY",
            "DRAFT_ONLY",
            "SAME_CHANGE",
            "CONFLICT",
        }
        assert changes["identity.temp_credential_ttl"] == "CURRENT_ONLY"
        assert changes["identity.password_max_age"] == "SAME_CHANGE"
        assert changes["identity.session_cap"] == "CONFLICT"
        assert changes["identity.session_idle_timeout"] == "DRAFT_ONLY"
    else:
        assert changes == {
            ACCEPTANCE_KEY: "SAME_CHANGE",
            FORMAL_KEY: "DRAFT_ONLY",
            ARCHIVE_KEY: "CONFLICT",
        }
    h.owner.version_snapshot.assert_called_once_with(namespace, "PLATFORM", 1)
    h.owner.active_snapshot.assert_called_once_with(namespace)
    h.owner.draft.assert_called_once_with(h.draft.id)
    assert (h.owner.draft.return_value, h.base, h.current, h.catalog) == before
    mutable = next(item for item in result.items if isinstance(item.draft_value, (dict, list)))
    if isinstance(mutable.draft_value, list):
        mutable.draft_value.append("only-in-response")
    else:
        mutable.draft_value["failureThreshold"] = 99
    assert (h.owner.draft.return_value, h.base, h.current, h.catalog) == before
    assert_read_only(h)


def test_requirement_sets_use_only_legal_member_and_cover_current_only_and_unchanged() -> None:
    h = comparison_state("requirement.gate")
    h.owner.draft.return_value = h.draft.model_copy(
        update={"content": deepcopy(h.base.values), "content_hash": h.base.snapshot_hash}
    )
    values = {**deepcopy(h.base.values), ACCEPTANCE_KEY: ["code.change"]}
    h.owner.active_snapshot.return_value = h.current.model_copy(
        update={"values": values, "snapshot_hash": _content_hash(values)}
    )
    assert {item.key: item.change for item in compare(h).items} == {
        ACCEPTANCE_KEY: "CURRENT_ONLY",
        FORMAL_KEY: "UNCHANGED",
        ARCHIVE_KEY: "UNCHANGED",
    }


def test_same_version_and_object_key_order_are_equal_and_object_conflicts_are_atomic() -> None:
    h = comparison_state()
    values = deepcopy(h.base.values)
    values["identity.login_backoff"] = dict(
        reversed(list(values["identity.login_backoff"].items()))
    )
    h.owner.active_snapshot.return_value = h.base.model_copy(update={"values": values})
    h.owner.draft.return_value = h.draft.model_copy(
        update={"content": deepcopy(values), "content_hash": _content_hash(values)}
    )
    assert {item.change for item in compare(h).items} == {"UNCHANGED"}
    current, draft = deepcopy(values), deepcopy(values)
    current["identity.login_backoff"]["initialDelaySeconds"] = 60
    draft["identity.login_backoff"]["maximumDelaySeconds"] = 1200
    h.owner.active_snapshot.return_value = h.current.model_copy(
        update={"values": current, "snapshot_hash": _content_hash(current)}
    )
    h.owner.draft.return_value = h.draft.model_copy(
        update={"content": draft, "content_hash": _content_hash(draft)}
    )
    item = next(item for item in compare(h).items if item.key == "identity.login_backoff")
    assert (
        item.change == "CONFLICT"
        and item.current_value == current[item.key]
        and item.draft_value == draft[item.key]
    )


@pytest.mark.parametrize(
    "case,error",
    [
        ("missing-draft", DraftNotFound),
        ("wrong-namespace", DraftNotFound),
        ("revision", StaleDraftRevision),
        ("missing-base", PolicySnapshotUnavailable),
        ("draft-hash", PolicySnapshotUnavailable),
        ("draft-schema", PolicySnapshotUnavailable),
        ("draft-scope", PolicySnapshotUnavailable),
        ("draft-status", PolicySnapshotUnavailable),
        ("empty-owner", PolicySnapshotUnavailable),
        ("base-version", PolicySnapshotUnavailable),
        ("current-version", PolicySnapshotUnavailable),
        ("base-namespace", PolicySnapshotUnavailable),
        ("current-scope", PolicySnapshotUnavailable),
        ("current-schema", PolicySnapshotUnavailable),
        ("base-hash", PolicySnapshotUnavailable),
        ("current-hash", PolicySnapshotUnavailable),
        ("same-version-conflict", PolicySnapshotUnavailable),
        ("catalog-duplicate", PolicySnapshotUnavailable),
        ("catalog-schema", PolicySnapshotUnavailable),
        ("catalog-namespace", PolicySnapshotUnavailable),
        ("catalog-incomplete", PolicySnapshotUnavailable),
        ("invalid-draft", InvalidPolicyValue),
        ("invalid-base", PolicySnapshotUnavailable),
        ("invalid-current", PolicySnapshotUnavailable),
    ],
)
def test_comparison_rejects_missing_stale_or_inconsistent_inputs_without_partial_result(
    case: str,
    error: type[Exception],
) -> None:
    h = comparison_state()
    if case == "missing-draft":
        h.owner.draft.return_value = None
    if case == "wrong-namespace":
        h.owner.draft.return_value = h.draft.model_copy(update={"namespace": "other"})
    if case == "revision":
        h.owner.draft.return_value = h.draft.model_copy(update={"revision": 8})
    if case == "missing-base":
        h.owner.version_snapshot.return_value = None
    for prefix, value in (("draft", h.draft), ("base", h.base), ("current", h.current)):
        updated = value
        field = case.removeprefix(prefix + "-")
        changes: dict[str, Any] = {}
        if case.startswith(prefix + "-"):
            if field == "hash":
                changes["content_hash" if prefix == "draft" else "snapshot_hash"] = "a" * 64
            if field == "schema":
                changes["schema_revision"] = 2
            if field == "scope":
                changes["scope"] = "WORKSPACE"
            if field == "namespace":
                changes["namespace"] = "other"
            if field == "status":
                changes["status"] = "UNKNOWN"
            if field == "version":
                changes["version"] = 0 if prefix == "current" else 3
        if case == "invalid-" + prefix:
            invalid = {
                **deepcopy(value.content if prefix == "draft" else value.values),
                "identity.session_cap": True,
            }
            changes = {
                "content" if prefix == "draft" else "values": invalid,
                "content_hash" if prefix == "draft" else "snapshot_hash": _content_hash(invalid),
            }
        if changes:
            updated = value.model_copy(update=changes)
        if updated is not value:
            getattr(
                h.owner,
                {"draft": "draft", "base": "version_snapshot", "current": "active_snapshot"}[
                    prefix
                ],
            ).return_value = updated
    if case == "empty-owner":
        h.owner.draft.return_value = h.draft.model_copy(update={"owner_id": ""})
    if case == "same-version-conflict":
        h.owner.active_snapshot.return_value = h.current.model_copy(update={"version": 1})
    if case == "catalog-duplicate":
        h.owner.catalog.return_value = h.catalog + [h.catalog[0]]
    if case == "catalog-schema":
        h.owner.catalog.return_value = [
            item.model_copy(update={"schema_revision": 2}) for item in h.catalog
        ]
    if case == "catalog-namespace":
        h.owner.catalog.return_value = [
            item.model_copy(update={"namespace": "other"}) for item in h.catalog
        ]
    if case == "catalog-incomplete":
        h.owner.catalog.return_value = h.catalog[:-1]
    with pytest.raises(error):
        compare(h)
    assert_read_only(h)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_actual_owner_normalization_preserves_supported_json_and_rejects_bad_schema_or_values(
    namespace: str,
) -> None:
    h = comparison_state(namespace)
    db = Mock()
    db.execute.return_value.mappings.return_value = [key.model_dump() for key in h.catalog]
    owner: Any = (
        IdentityPolicyOwner(db) if namespace == "identity" else SqlAlchemyGatePolicyRepository(db)
    )
    raw = deepcopy(h.draft.content)
    if namespace == "requirement.gate":
        raw[ACCEPTANCE_KEY] = ("code.change",)
    before = deepcopy(raw)
    assert hasattr(owner, "normalize_candidate"), "actual owner normalizer is missing"
    normalized = owner.normalize_candidate(namespace, schema_revision=1, values=raw)
    assert normalized == h.draft.content
    assert raw == before and normalized is not raw
    mutable = next(value for value in normalized.values() if isinstance(value, (dict, list)))
    if isinstance(mutable, list):
        mutable.append("only-in-copy")
    else:
        mutable["failureThreshold"] = 99
    assert raw == before
    with pytest.raises(PolicySnapshotUnavailable):
        owner.normalize_candidate(namespace, schema_revision=2, values=raw)
    invalid = deepcopy(raw)
    invalid["identity.session_cap" if namespace == "identity" else ARCHIVE_KEY] = True
    with pytest.raises(InvalidPolicyValue):
        owner.normalize_candidate(namespace, schema_revision=1, values=invalid)
    assert all(
        str(call.args[0]).lstrip().startswith("SELECT") for call in db.execute.call_args_list
    )


@pytest.fixture(params=["identity", "requirement.gate"])
def comparison_api(request: pytest.FixtureRequest) -> Any:
    h = comparison_state(request.param)
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        try:
            yield h.lifecycle
        finally:
            h.exits += 1

    h.owners, h.guard, h.secrets = Mock(), Mock(), Mock()
    h.owners.resolve.return_value = SimpleNamespace(transaction=transaction)
    runtime = ConfigurationHttpRuntime(h.owners, h.dependencies, h.secrets)
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: runtime, lambda: SimpleNamespace(account_id="different-admin"), h.guard
        )
    )
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts/{h.draft.id}/base-comparison"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        h.app = app
        yield h


def test_comparison_http_is_read_only_get_with_target_etag_and_no_store(
    comparison_api: Any,
) -> None:
    h = comparison_api
    response = h.client.get(h.path, headers={"If-Match": '"v7"', "Origin": "https://other.example"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["draftId"] == h.draft.id and value["ownerId"] == "other-admin"
    assert value["draftRevision"] == 7 and value["currentVersion"] == 2
    assert response.headers["etag"] == '"v7"' and response.headers["cache-control"] == "no-store"
    assert h.exits == 1
    h.secrets.load.assert_not_called()
    assert_read_only(h)
    schema = h.app.openapi()
    route = schema["paths"]["/api/v1/admin/policies/{namespace}/drafts/{draft_id}/base-comparison"][
        "get"
    ]
    assert route["operationId"] == "draft_base_comparison"
    parameters = {item["name"]: item for item in route["parameters"]}
    assert parameters["If-Match"]["required"] is True
    assert "Idempotency-Key" not in parameters and "requestBody" not in route
    assert (
        schema["components"]["schemas"]["DraftBaseComparisonResponseDto"]["additionalProperties"]
        is False
    )


@pytest.mark.parametrize("status", [401, 403, 503])
def test_comparison_authorization_failure_precedes_any_owner_access(
    comparison_api: Any, status: int
) -> None:
    h = comparison_api
    h.guard.side_effect = HTTPException(status_code=status)
    assert h.client.get(h.path, headers={"If-Match": '"v7"'}).status_code == status
    h.owners.resolve.assert_not_called()
    assert h.owner.mock_calls == [] and h.exits == 0


@pytest.mark.parametrize("etag", [None, 'W/"v7"', '"v0"', '"v07"', "bad"])
def test_comparison_rejects_missing_or_malformed_if_match(
    comparison_api: Any, etag: str | None
) -> None:
    h = comparison_api
    assert (
        h.client.get(h.path, headers={} if etag is None else {"If-Match": etag}).status_code == 422
    )
    h.owners.resolve.assert_not_called()


@pytest.mark.parametrize(
    "case,status",
    [("missing", 404), ("revision", 409), ("candidate", 422), ("read", 503), ("base", 503)],
)
def test_comparison_http_failure_releases_transaction_without_leaking_values(
    comparison_api: Any, case: str, status: int
) -> None:
    h = comparison_api
    if case == "missing":
        h.owner.draft.return_value = None
    if case == "revision":
        h.owner.draft.return_value = h.draft.model_copy(update={"revision": 8})
    if case == "candidate":
        raw = {
            **h.draft.content,
            "identity.session_cap" if h.namespace == "identity" else ARCHIVE_KEY: True,
        }
        h.owner.draft.return_value = h.draft.model_copy(
            update={"content": raw, "content_hash": _content_hash(raw)}
        )
    if case == "read":
        h.owner.active_snapshot.side_effect = RuntimeError("private-fault-sentinel")
    if case == "base":
        h.owner.version_snapshot.return_value = None
    response = h.client.get(h.path, headers={"If-Match": '"v7"'})
    assert response.status_code == status, response.text
    assert (
        "private-fault-sentinel" not in response.text
        and "identity.login_backoff" not in response.text
    )
    assert h.exits == 1
    assert_read_only(h)
