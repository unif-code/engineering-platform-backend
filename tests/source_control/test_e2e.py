import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from control_plane.app.bootstrap.source_control_connector import (
    create_source_control_connector_app,
)
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.source_control import (
    EffectState,
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    register_workspace_repository,
)
from control_plane.app.modules.source_control.adapters import (
    RequirementFacadeBindingAdapter,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.api import SourceControlWebhookRuntime
from control_plane.app.modules.source_control.ports import (
    GitLabProviderUnavailable,
    GitLabResultUnknown,
)
from control_plane.tools.source_control_worker import main as worker_main
from control_plane.tools.source_control_worker import run_worker_once
from tests.requirement.conftest import (
    IsolatedRequirementDatabase,
    isolated_requirement_database,
    requirement_owner_engine,
)
from tests.requirement.test_source_control_relay import (
    WORKSPACE_ID,
    create_assigned_requirement,
)
from tests.requirement.test_source_control_relay import (
    dependencies as requirement_dependencies,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_commands import (
    FakeEligibility,
    FakeGitLab,
    FixedClock,
    FixedPolicy,
    FixedRandom,
)

REPOSITORY_ID = "repository-source-control-1"
SIGNING_KEY = b"test-only-e2e-signing-key-32-byte"
SIGNING_TOKEN = "whsec_" + base64.b64encode(SIGNING_KEY).decode("ascii")

# Imported so pytest registers the cross-module PostgreSQL fixtures used by this E2E.
assert isolated_requirement_database and requirement_owner_engine


def _unavailable_dependencies() -> SourceControlDependencies:
    raise SourceControlDependencyUnavailable("do-not-print-secret-value")


class FakeSecrets:
    def resolve(self, reference: str) -> str:
        assert reference == "secret-ref:webhook"
        return SIGNING_TOKEN


def _dependencies(
    source: IsolatedSourceControlDatabase,
    requirement: IsolatedRequirementDatabase,
    gitlab: FakeGitLab,
) -> SourceControlDependencies:
    clock = FixedClock()
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=source.runtime,
        requirement=RequirementFacadeBindingAdapter(
            requirement.runtime,
            requirement_dependencies(),
            clock,
        ),
        eligibility=FakeEligibility(),
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=clock,
        random=FixedRandom(),
        gitlab=gitlab,
        policy=FixedPolicy(),
        webhook_secrets=FakeSecrets(),
    )


def _register(
    source: IsolatedSourceControlDatabase,
    dependencies: SourceControlDependencies,
) -> None:
    with source.runtime.begin() as db:
        register_workspace_repository(
            SqlAlchemySourceControlRepository(db),
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref="secret-ref:webhook",
            actor="SYSTEM",
            dependencies=dependencies,
        )


def _signed_push_headers(body: bytes, webhook_id: str) -> dict[str, str]:
    timestamp = int(FixedClock().now().timestamp())
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    signature = base64.b64encode(hmac.new(SIGNING_KEY, signed, hashlib.sha256).digest())
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": "v1," + signature.decode("ascii"),
        "x-gitlab-event": "Push Hook",
        "x-gitlab-event-uuid": f"provider-{webhook_id}",
    }


def _assert_bound_requirement(
    requirement: IsolatedRequirementDatabase,
    *,
    work_item_id: str,
    branch_name: str,
) -> None:
    with requirement.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT repository_state, repository_blocked_reason_code, "
                "base_commit_sha, task_branch FROM requirement.work_item WHERE id=:id"
            ),
            {"id": work_item_id},
        ).one()
    assert row == ("BOUND", None, "a" * 40, branch_name)


def test_worker_happy_path_converges_duplicate_delivery_without_duplicate_facts(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = create_assigned_requirement(isolated_requirement_database)
    gitlab = FakeGitLab()
    dependencies = _dependencies(
        isolated_source_control_database,
        isolated_requirement_database,
        gitlab,
    )
    _register(isolated_source_control_database, dependencies)

    relayed = run_worker_once("relay", limit=10, dependencies=dependencies)
    processed = run_worker_once("process", limit=10, dependencies=dependencies)
    duplicate_relay = run_worker_once("relay", limit=10, dependencies=dependencies)
    duplicate_process = run_worker_once("process", limit=10, dependencies=dependencies)

    assert (relayed.claimed, relayed.processed, relayed.released) == (1, 1, 0)
    assert (processed.claimed, processed.processed) == (1, 1)
    assert duplicate_relay.claimed == duplicate_process.claimed == 0
    assert len(processed.effect_ids) == 1
    with isolated_source_control_database.owner.connect() as db:
        effect = db.execute(
            text(
                "SELECT state, branch_name FROM source_control.source_control_effect "
                "WHERE work_item_id=:work_item_id"
            ),
            {"work_item_id": created.work_item.id},
        ).one()
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM source_control.binding_request_inbox), "
                "(SELECT count(*) FROM source_control.source_control_effect), "
                "(SELECT count(*) FROM source_control.repository_branch_binding)"
            )
        ).one()
    assert effect.state == EffectState.SUCCEEDED.value
    assert counts == (1, 1, 1)
    assert gitlab.calls == [
        ("GET", "main"),
        ("POST", effect.branch_name),
        ("GET", effect.branch_name),
    ]
    assert gitlab.created == [(effect.branch_name, "a" * 40)]
    _assert_bound_requirement(
        isolated_requirement_database,
        work_item_id=created.work_item.id,
        branch_name=effect.branch_name,
    )


def test_signed_webhook_only_schedules_unknown_effect_then_reconciliation_binds(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = create_assigned_requirement(isolated_requirement_database)
    gitlab = FakeGitLab()
    gitlab.create_error = GitLabResultUnknown("timeout")
    gitlab.task_read_error = GitLabProviderUnavailable("unavailable")
    dependencies = _dependencies(
        isolated_source_control_database,
        isolated_requirement_database,
        gitlab,
    )
    _register(isolated_source_control_database, dependencies)
    run_worker_once("relay", limit=10, dependencies=dependencies)

    unknown = run_worker_once("process", limit=10, dependencies=dependencies)
    assert unknown.error_codes == ("RECONCILIATION_PENDING",)
    with isolated_source_control_database.owner.connect() as db:
        effect = db.execute(
            text(
                "SELECT id, state, branch_name FROM source_control.source_control_effect "
                "WHERE work_item_id=:work_item_id"
            ),
            {"work_item_id": created.work_item.id},
        ).one()
        binding_count = db.execute(
            text("SELECT count(*) FROM source_control.repository_branch_binding")
        ).scalar_one()
    assert (effect.state, binding_count) == (EffectState.UNKNOWN.value, 0)

    body = json.dumps(
        {
            "object_kind": "push",
            "project": {"id": 101},
            "ref": f"refs/heads/{effect.branch_name}",
            "before": "0" * 40,
            "after": "a" * 40,
            "checkout_sha": "a" * 40,
        },
        separators=(",", ":"),
    ).encode()
    connector = TestClient(
        create_source_control_connector_app(
            runtime_provider=lambda: SourceControlWebhookRuntime(dependencies)
        )
    )
    headers = _signed_push_headers(body, "e2e-webhook-1")
    first = connector.post(f"/webhooks/gitlab/{REPOSITORY_ID}", content=body, headers=headers)
    duplicate = connector.post(f"/webhooks/gitlab/{REPOSITORY_ID}", content=body, headers=headers)
    assert first.status_code == duplicate.status_code == 202
    assert first.json() == duplicate.json()

    webhook_run = run_worker_once("process", limit=10, dependencies=dependencies)
    with isolated_source_control_database.owner.connect() as db:
        before_reconcile = db.execute(
            text(
                "SELECT state, (SELECT count(*) FROM source_control.repository_branch_binding) "
                "FROM source_control.source_control_effect WHERE id=:id"
            ),
            {"id": effect.id},
        ).one()
    assert webhook_run.processed == 1
    assert before_reconcile == (EffectState.UNKNOWN.value, 0)

    gitlab.create_error = None
    gitlab.task_read_error = None
    gitlab.branch_sha = "a" * 40
    reconciled = run_worker_once("reconcile", limit=10, dependencies=dependencies)

    assert reconciled.effect_ids == (str(effect.id),)
    _assert_bound_requirement(
        isolated_requirement_database,
        work_item_id=created.work_item.id,
        branch_name=effect.branch_name,
    )
    with isolated_source_control_database.owner.connect() as db:
        facts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM source_control.webhook_inbox), "
                "(SELECT count(*) FROM source_control.repository_branch_binding), "
                "(SELECT state FROM source_control.source_control_effect WHERE id=:id)"
            ),
            {"id": effect.id},
        ).one()
    assert facts == (1, 1, EffectState.SUCCEEDED.value)


def test_runtime_roles_cannot_cross_write_and_audit_contains_no_secret_or_body(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    gitlab = FakeGitLab()
    dependencies = _dependencies(
        isolated_source_control_database,
        isolated_requirement_database,
        gitlab,
    )
    _register(isolated_source_control_database, dependencies)

    with isolated_source_control_database.runtime.begin() as db, pytest.raises(DBAPIError):
        db.execute(text("INSERT INTO requirement.requirement (id) VALUES ('forbidden')"))
    with isolated_requirement_database.runtime.begin() as db, pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM source_control.workspace_repository"))
    with isolated_source_control_database.owner.connect() as db:
        serialized = " ".join(
            str(value)
            for row in db.execute(
                text(
                    "SELECT actor, action, target_id, reason, correlation_id FROM audit.audit_event"
                )
            )
            for value in row
            if value is not None
        )
    assert REPOSITORY_ID in serialized
    assert "whsec_" not in serialized
    assert "object_kind" not in serialized


def test_unassembled_connector_and_worker_fail_closed_without_leaking_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    connector = TestClient(create_source_control_connector_app())

    assert connector.get("/healthz").status_code == 200
    assert connector.get("/readyz").status_code == 503
    response = connector.post("/webhooks/gitlab/repository-1", content=b"secret-body")
    assert response.status_code == 503
    assert "secret-body" not in response.text

    exit_code = worker_main(
        ["relay", "--limit", "1"],
        dependencies_provider=_unavailable_dependencies,
    )
    output = capsys.readouterr().out
    assert exit_code == 1
    assert json.loads(output) == {
        "command": "relay",
        "errorCodes": ["DEPENDENCY_UNAVAILABLE"],
    }
    assert "do-not-print-secret-value" not in output
