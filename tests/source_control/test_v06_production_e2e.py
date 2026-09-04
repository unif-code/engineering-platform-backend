"""Default HTTP/CLI composition: no business-owner doubles or shared-DB cleanup."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pyotp
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.bootstrap.source_control_runtime import (
    SourceControlRuntime,
    build_source_control_runtime,
    default_source_control_collaborators,
)
from control_plane.app.modules.identity.adapters.runtime import SystemClock
from control_plane.app.modules.source_control import process_formal_delivery_request
from control_plane.app.modules.source_control.adapters import SourceControlDevSettings
from control_plane.tools import bootstrap_admin, source_control_repository, source_control_worker
from tests.integration_database import migration_database_url, required_engine
from tests.test_e2e_access_governance import SAME_ORIGIN, _initialize, _runtime_engine

pytestmark = pytest.mark.integration
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
INTEGRATION_SHA = "c" * 40
FORMAL_SHA = "d" * 40


def _clear_composition() -> None:
    for name in (
        "identity_dependencies",
        "organization_dependencies",
        "workspace_dependencies",
        "authorization_dependencies",
        "configuration_dependencies",
        "requirement_dependencies",
        "requirement_policy_runtime",
        "actor_qualification_runtime",
        "source_control_dependencies",
        "identity_http_runtime",
        "authorization_http_runtime",
        "organization_http_runtime",
        "workspace_http_runtime",
        "configuration_http_runtime",
        "requirement_http_runtime",
        "source_control_query_runtime",
        "security_change_orchestrator",
    ):
        getattr(bootstrap, name).cache_clear()


@dataclass
class ProductionDatabase:
    owner: Engine
    engines: dict[str, Engine]


@pytest.fixture
def production_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ProductionDatabase]:
    """Create/migrate/drop only a verified UUID-named database, with real owner roles."""
    owner_url = migration_database_url()
    maintenance = required_engine(
        owner_url.set(database="postgres"), role="platform_owner", minimum_server_version=180000
    )
    database_name = f"test_v06_production_{uuid4().hex}"
    assert database_name.startswith("test_v06_production_") and database_name != owner_url.database
    target_url = owner_url.set(database=database_name)
    isolated_owner = create_engine(target_url, pool_pre_ping=True)
    created = False
    _clear_composition()
    try:
        with maintenance.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
            db.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True
        monkeypatch.setenv(
            "MIGRATION_DATABASE_URL", target_url.render_as_string(hide_password=False)
        )
        command.upgrade(Config("alembic.ini"), "heads")
        with isolated_owner.connect() as db:
            assert db.execute(text("SELECT current_database()")).scalar_one() == database_name
        secret_dir = tmp_path / "identity-secrets"
        secret_dir.mkdir()
        for name in ("pepper", "totp_key", "idempotency_key"):
            (secret_dir / name).write_bytes(uuid4().bytes + uuid4().bytes)
        monkeypatch.setenv("SECRET_MATERIAL_PATH", str(secret_dir))
        with ExitStack() as stack:
            engines = {
                name: stack.enter_context(
                    _runtime_engine(isolated_owner, target_url, privilege_role=f"{name}_rw")
                )
                for name in (
                    "audit",
                    "identity",
                    "organization",
                    "workspace",
                    "authorization",
                    "requirement",
                    "source_control",
                )
            }
            for name, factory in {
                "audit": "runtime_engine",
                "identity": "identity_runtime_engine",
                "organization": "organization_runtime_engine",
                "workspace": "workspace_runtime_engine",
                "authorization": "authorization_runtime_engine",
                "requirement": "requirement_runtime_engine",
                "source_control": "source_control_query_runtime_engine",
            }.items():
                monkeypatch.setattr(bootstrap, factory, lambda name=name: engines[name])
            _clear_composition()
            try:
                yield ProductionDatabase(isolated_owner, engines)
            finally:
                _clear_composition()
    finally:
        isolated_owner.dispose()
        if created:
            with maintenance.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
                db.execute(text(f'DROP DATABASE "{database_name}"'))
        maintenance.dispose()


class GitLabTransport:
    """Stateful external HTTP boundary, exercised by the production GitLab adapters."""

    def __init__(self) -> None:
        self.branches = {"main": BASE_SHA, "dev": BASE_SHA}
        self.mrs: dict[int, dict[str, Any]] = {}
        self.writes: list[tuple[str, str]] = []
        self.timeout_formal_create = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        assert request.headers["PRIVATE-TOKEN"] == "test-only-pat"
        if request.method != "GET":
            self.writes.append((request.method, path))
        if path.endswith("/repository/branches") and request.method == "POST":
            assert params["ref"] == BASE_SHA
            self.branches[params["branch"]] = params["ref"]
            return httpx.Response(
                201, json={"name": params["branch"], "commit": {"id": params["ref"]}}
            )
        if "/repository/branches/" in path:
            name = path.split("/repository/branches/", 1)[1]
            if name not in self.branches:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "name": name,
                    "commit": {"id": self.branches[name]},
                    "protected": name in {"main", "dev"},
                },
            )
        if path.endswith("/merge_requests"):
            source, target = params["source_branch"], params["target_branch"]
            if request.method == "GET":
                assert params["state"] == "all"
                for mr in self.mrs.values():
                    if mr["state"] == "opened":
                        mr["sha"] = self.branches[mr["source_branch"]]
                        mr["diff_refs"] = {"head_sha": mr["sha"]}
                return httpx.Response(
                    200,
                    json=[
                        mr
                        for mr in self.mrs.values()
                        if mr["source_branch"] == source and mr["target_branch"] == target
                    ],
                )
            formal = target == "main"
            assert params["squash"] == str(formal).lower()
            assert params["remove_source_branch"] == str(formal).lower()
            assert params["allow_collaboration"] == "false"
            iid = len(self.mrs) + 1
            self.mrs[iid] = {
                "iid": iid,
                "source_branch": source,
                "target_branch": target,
                "sha": self.branches[source],
                "diff_refs": {"head_sha": self.branches[source]},
                "state": "opened",
                "detailed_merge_status": "mergeable",
                "has_conflicts": False,
                "blocking_discussions_resolved": True,
                "head_pipeline": {"status": "success"},
                "merge_commit_sha": None,
                "merge_user": None,
                "merged_at": None,
            }
            if formal and self.timeout_formal_create:
                self.timeout_formal_create = False
                raise httpx.ReadTimeout("Test external response lost after create", request=request)
            return httpx.Response(201, json=self.mrs[iid])
        if "/merge_requests/" in path:
            iid = int(path.split("/merge_requests/", 1)[1].split("/")[0])
            mr = self.mrs[iid]
            if request.method == "PUT":
                formal = mr["target_branch"] == "main"
                assert params["sha"] == mr["sha"]
                assert params["squash"] == str(formal).lower()
                assert params["should_remove_source_branch"] == str(formal).lower()
                sha = (
                    FORMAL_SHA
                    if formal
                    else {HEAD_SHA: INTEGRATION_SHA, "e" * 40: "f" * 40, "1" * 40: "2" * 40}[
                        mr["sha"]
                    ]
                )
                mr.update(state="merged", merge_commit_sha=sha, merged_at="2026-09-04T01:00:00Z")
                self.branches[mr["target_branch"]] = sha
            return httpx.Response(200, json=mr)
        if request.method == "GET" and (
            path.endswith("/projects/platform/backend") or path.endswith("/projects/301")
        ):
            return httpx.Response(
                200,
                json={
                    "id": 301,
                    "path_with_namespace": "platform/backend",
                    "default_branch": "main",
                    "merge_method": "merge",
                },
            )
        raise AssertionError(f"Unexpected provider request: {request.method} {path}")


@dataclass
class Journey:
    database: ProductionDatabase
    admin: TestClient
    leader: TestClient
    member: TestClient
    manager_id: str
    leader_id: str
    member_id: str
    workspace_id: str
    repository_id: str
    provider: GitLabTransport
    settings: SourceControlDevSettings
    admin_totp: str = field(repr=False)

    def worker(self, lane: str, *, errors: tuple[str, ...] = ()) -> dict[str, Any]:
        @contextmanager
        def runtime_context() -> Iterator[SourceControlRuntime]:
            runtime = build_source_control_runtime(
                self.settings,
                collaborators=default_source_control_collaborators(),
                client_factory=lambda **kwargs: httpx.Client(
                    transport=httpx.MockTransport(self.provider), **kwargs
                ),
            )
            try:
                runtime.ensure_ready()
                yield runtime
            finally:
                runtime.close()
                assert runtime.client.is_closed

        output = StringIO()
        with redirect_stdout(output):
            code = source_control_worker.main(
                [lane, "--limit", "40"], runtime_context_provider=runtime_context
            )
        assert code == 0, output.getvalue()
        report = json.loads(output.getvalue())
        assert report["error_codes"] == list(errors), report
        return cast(dict[str, Any], report)


def _write(
    client: TestClient,
    path: str,
    body: dict[str, Any],
    *,
    status: int = 200,
    etag: str | None = None,
    method: str = "POST",
    key: str | None = None,
) -> httpx.Response:
    headers = {**SAME_ORIGIN, "Idempotency-Key": key or str(uuid4())}
    if etag is not None:
        headers["If-Match"] = etag
    result = client.request(method, path, json=body, headers=headers)
    assert result.status_code == status, result.text
    return cast(httpx.Response, result)


def _grant(
    admin: TestClient, account_id: str, capability: str, workspace: str | None = None
) -> None:
    _write(
        admin,
        "/api/v1/admin/grants",
        {
            "principalId": account_id,
            "capability": capability,
            "scopeType": "WORKSPACE" if workspace else "PLATFORM",
            "scopeId": workspace,
            "source": "MANUAL",
            "reason": "Production E2E explicit authority",
        },
        status=201,
    )


@pytest.fixture
def journey(production_database: ProductionDatabase, tmp_path: Path) -> Iterator[Journey]:
    engines = production_database.engines
    stdout, stderr = StringIO(), StringIO()
    assert (
        bootstrap_admin.main(
            ["--employee-no", "00000001", "--display-name", "Administrator"],
            engine=engines["identity"],
            dependencies=bootstrap.identity_dependencies(),
            security_changes=bootstrap.security_change_orchestrator(),
            authorization_engine=engines["authorization"],
            authorization_dependencies=bootstrap.authorization_dependencies(),
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    app = bootstrap.create_app()
    with ExitStack() as stack:
        clients = [
            stack.enter_context(TestClient(app, base_url="https://testserver")) for _ in range(4)
        ]
        admin, manager, leader, member = clients
        admin_totp = _initialize(
            admin,
            employee_no="00000001",
            temporary_password=stdout.getvalue().strip(),
            password="V06Admin!Password#2026",
            key="init-admin",
            request_ids={
                name: f"req-admin{name}" for name in ("login", "password", "enroll", "confirm")
            },
        )
        ids = []
        for index, client in enumerate((manager, leader, member), start=2):
            created = _write(
                admin,
                "/api/v1/admin/accounts",
                {
                    "employeeNo": f"{index:08}",
                    "displayName": f"Actor {index}",
                    "profession": "BACKEND",
                    "reason": "Production lifecycle",
                },
                status=201,
            ).json()
            ids.append(created["account"]["id"])
            _initialize(
                client,
                employee_no=f"{index:08}",
                temporary_password=created["temporaryPassword"],
                password=f"V06Actor!Password#{index:04}",
                key=f"init-{index}",
                request_ids={
                    name: f"req-actor{index}{name}"
                    for name in ("login", "password", "enroll", "confirm")
                },
            )
        manager_id, leader_id, member_id = ids
        admin_id = admin.get("/api/v1/me").json()["accountId"]
        _grant(admin, admin_id, "platform.organization.manage")
        for account, superior in (
            (manager_id, None),
            (leader_id, manager_id),
            (member_id, leader_id),
        ):
            _write(
                admin,
                f"/api/v1/admin/accounts/{account}/superior",
                {"superiorId": superior, "reason": "Real organization lifecycle"},
                status=204,
                method="PUT",
            )
        for capability in ("platform.workspace.manage", "platform.workspace.read"):
            _grant(admin, leader_id, capability)
        workspace = _write(
            leader,
            "/api/v1/admin/workspaces",
            {
                "name": "V06 production proof",
                "ownerId": leader_id,
                "reason": "Production lifecycle",
            },
            status=201,
        ).json()
        workspace_id = workspace["id"]
        _grant(admin, leader_id, "platform.workspace.read", workspace_id)
        members = leader.get(f"/api/v1/admin/workspaces/{workspace_id}/members")
        assert members.status_code == 200, members.text
        assert {item["accountId"] for item in members.json()["items"]} == {leader_id, member_id}
        for account in (leader_id, member_id):
            for capability in (
                "requirement.create",
                "requirement.read",
                "code.change",
                "work_item.create",
                "work_item.assign",
                "work_item.execute",
                "requirement.baseline.submit",
                "requirement.baseline.assign",
                "requirement.baseline.decide",
                "work_item.validation.submit",
                "requirement.evidence.request",
                "requirement.evidence.select",
                "requirement.acceptance.submit",
                "requirement.acceptance.decide",
                "requirement.delivery_gate.assign",
                "formal_merge_request.request",
                "merge_request.review",
                "merge_request.merge",
            ):
                _grant(admin, account, capability, workspace_id)
        repository_id = str(uuid4())
        output, errors = StringIO(), StringIO()
        assert (
            source_control_repository.main(
                [
                    "register",
                    "--repository-id",
                    repository_id,
                    "--workspace-id",
                    workspace_id,
                    "--project-id",
                    "301",
                    "--project-path",
                    "platform/backend",
                    "--connection-ref",
                    "gitlab-dev",
                    "--credential-secret-ref",
                    "secret-ref:gitlab-pat",
                    "--webhook-signing-secret-ref",
                    "secret-ref:gitlab-webhook",
                ],
                engine=engines["source_control"],
                dependencies=bootstrap.source_control_dependencies(),
                stdout=output,
                stderr=errors,
            )
            == 0
        )
        assert errors.getvalue() == ""
        secret_root = tmp_path / "gitlab-secrets"
        secret_root.mkdir()
        (secret_root / "gitlab-pat").write_text("test-only-pat")
        (secret_root / "gitlab-webhook").write_text("test-only-webhook")
        settings = SourceControlDevSettings.model_validate(
            {
                "gitlab_api_url": "https://gitlab.example.test/api/v4",
                "connection_id": "gitlab-dev",
                "request_timeout_seconds": 5,
                "policy_version": 1,
                "reconcile_base_delay_seconds": 15,
                "reconcile_max_delay_seconds": 120,
                "webhook_replay_window_seconds": 300,
                "secret_reference_root": secret_root,
            }
        )
        yield Journey(
            production_database,
            admin,
            leader,
            member,
            manager_id,
            leader_id,
            member_id,
            workspace_id,
            repository_id,
            GitLabTransport(),
            settings,
            admin_totp,
        )


@dataclass
class Subject:
    base: str
    work: str
    work_item_id: str
    artifact: dict[str, Any]
    peers: tuple[str, ...] = ()


def _ready_subject(journey: Journey, *, count: int = 1) -> Subject:
    created = _write(
        journey.member,
        "/api/v1/requirements",
        {
            "workspaceId": journey.workspace_id,
            "type": "feat",
            "title": "Production V06 delivery",
            "description": "Public lifecycle only",
            "acceptanceCriteria": ["Exact evidence reaches formal delivery"],
            "initialRepositoryId": journey.repository_id,
        },
        status=201,
    )
    requirement_id = created.json()["requirement"]["id"]
    assert created.json()["workItem"]["assignmentState"] == "ASSIGNED"
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process")["processed"] == 1
    details = journey.member.get(f"/api/v1/requirements/{requirement_id}")
    assert details.status_code == 200
    assert details.json()["requirement"]["state"] == "PREPARING"
    assert details.json()["workItems"][0]["repositoryState"] == "BOUND"
    assert details.json()["workItems"][0]["baseCommitSha"] == BASE_SHA
    base = f"/api/v1/requirements/{requirement_id}"
    work_item_id = created.json()["workItem"]["id"]
    work = f"{base}/work-items/{work_item_id}"
    artifact = _write(
        journey.member,
        f"{base}/sdd-artifacts",
        {"content": "# SDD\n\nDeliver verified change."},
        status=201,
        etag=details.headers["etag"],
    )
    peers = []
    for _ in range(count - 1):
        added = _write(
            journey.member,
            f"{base}/work-items",
            {"repositoryId": journey.repository_id},
            status=201,
            etag=journey.member.get(base).headers["etag"],
        )
        peer = added.json()["workItem"]["id"]
        assert added.json()["workItem"]["humanOwnerId"] == journey.member_id
        assert added.json()["workItem"]["assignmentState"] == "ASSIGNED"
        peers.append(peer)
    if peers:
        assert journey.worker("relay")["processed"] == len(peers)
        assert journey.worker("process")["processed"] == len(peers)
    baseline = _write(
        journey.member,
        f"{base}/sdd-baselines",
        {"artifactId": artifact.json()["artifact"]["artifactId"], "artifactVersion": 1},
        status=201,
        etag=journey.member.get(base).headers["etag"],
    )
    confirmation = _write(
        journey.member,
        f"{base}/baseline-confirmations",
        {"sddBaselineId": baseline.json()["baseline"]["id"]},
        status=201,
        etag=baseline.headers["etag"],
    )
    approved = _write(
        journey.leader,
        f"{base}/baseline-decisions",
        {
            "gateId": confirmation.json()["gate"]["id"],
            "outcome": "APPROVED",
            "reason": "Leader approved the actual SDD",
        },
        etag=confirmation.headers["etag"],
    )
    assert approved.json()["requirement"]["state"] == "READY"
    _write(journey.member, f"{work}:start", {}, etag=approved.headers["etag"])
    for peer in peers:
        _write(
            journey.member,
            f"{base}/work-items/{peer}:start",
            {},
            etag=journey.member.get(base).headers["etag"],
        )
    return Subject(base, work, work_item_id, artifact.json()["artifact"], tuple(peers))


def _open_integration_mr(journey: Journey, subject: Subject, *, head: str = HEAD_SHA) -> None:
    base, work = subject.base, subject.work
    details = journey.member.get(base)
    # The contributor commits at the external provider boundary, not in owner tables.
    item = next(item for item in details.json()["workItems"] if item["id"] == subject.work_item_id)
    journey.provider.branches[item["taskBranch"]] = head
    _write(
        journey.member,
        f"{work}:request-integration-mr",
        {},
        status=202,
        etag=details.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process")["processed"] == 1
    current = journey.member.get(base)
    assert (
        next(item for item in current.json()["workItems"] if item["id"] == subject.work_item_id)[
            "integrationDeliveryState"
        ]
        == "MR_OPEN"
    )


def _merge_integration(journey: Journey, subject: Subject) -> None:
    base, work = subject.base, subject.work
    current = journey.member.get(base)
    _write(
        journey.leader,
        f"{work}:request-integration-merge",
        {},
        status=202,
        etag=current.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process")["processed"] == 1
    integrated = journey.member.get(base)
    assert (
        next(item for item in integrated.json()["workItems"] if item["id"] == subject.work_item_id)[
            "integrationDeliveryState"
        ]
        == "INTEGRATED"
    )


def _integrate(journey: Journey, subject: Subject, *, head: str = HEAD_SHA) -> None:
    _open_integration_mr(journey, subject, head=head)
    _merge_integration(journey, subject)


def _submit_validation(
    journey: Journey, subject: Subject, *, head: str, merge_sha: str
) -> httpx.Response:
    base, work = subject.base, subject.work
    integrated = journey.member.get(base)
    validation = _write(
        journey.member,
        f"{work}/external-validations",
        {
            "targetCommitSha": head,
            "integrationMergeCommitSha": merge_sha,
            "reference": "https://ci.example.test/build/1",
            "notes": "Exact integrated change validated",
            "artifactReferences": [
                {
                    "artifactId": subject.artifact["artifactId"],
                    "artifactVersion": "1",
                    "artifactHash": subject.artifact["sha256"],
                }
            ],
        },
        etag=integrated.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    return validation


def _open_acceptance(
    journey: Journey, subject: Subject, *, head: str = HEAD_SHA, merge_sha: str = INTEGRATION_SHA
) -> httpx.Response:
    base = subject.base
    for peer in subject.peers:
        _submit_validation(
            journey,
            replace(subject, work_item_id=peer, work=f"{base}/work-items/{peer}"),
            head=head,
            merge_sha=merge_sha,
        )
    validation = _submit_validation(journey, subject, head=head, merge_sha=merge_sha)
    frozen = _write(
        journey.member,
        f"{base}:request-integration-baseline",
        {"expectedRequirementVersion": validation.json()["requirement"]["requirementVersion"]},
        status=202,
        etag=validation.headers["etag"],
    )
    snapshot = frozen.json()["snapshot"]
    evidence_url = f"{base}/delivery-snapshots/{snapshot['id']}/integration-baseline"
    pending = journey.member.get(evidence_url)
    assert pending.status_code == 409, pending.text
    assert pending.json()["reason"] == "EVIDENCE_UNAVAILABLE_OR_STALE"
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process")["processed"] == 1
    discovered = journey.member.get(evidence_url)
    assert discovered.status_code == 200, discovered.text
    evidence = discovered.json()
    assert evidence["deliverySnapshotId"] == snapshot["id"]
    assert evidence["deliverySnapshotHash"] == snapshot["snapshotHash"]
    assert evidence["requirementId"] == snapshot["requirementId"]
    assert evidence["requirementVersion"] == snapshot["requirementVersion"]
    assert evidence["requiredWorkItemSetHash"] == snapshot["requiredWorkItemSetHash"]
    assert evidence["currentnessState"] == "CURRENT"
    assert evidence["currentnessReasons"] == []
    assert {item["workItemId"] for item in evidence["workItems"]} == set(snapshot["workItemIds"])
    for item in evidence["workItems"]:
        assert item["repositoryId"] == journey.repository_id
        assert item["taskCommitSha"] == head
        assert item["integrationMergeCommitSha"] == merge_sha
        assert item["artifactReferences"] == [
            {
                "artifactId": subject.artifact["artifactId"],
                "artifactVersion": str(subject.artifact["version"]),
                "artifactHash": subject.artifact["sha256"],
            }
        ]
    selected = _write(
        journey.member,
        f"{base}/integration-baseline-selections",
        {
            "deliverySnapshotId": frozen.json()["snapshot"]["id"],
            "integrationBaselineId": evidence["id"],
            "expectedRequirementVersion": frozen.json()["requirement"]["requirementVersion"],
        },
        etag=frozen.headers["etag"],
    )
    assert selected.json()["selection"]["integrationBaselineHash"] == evidence["evidenceHash"]
    return _write(
        journey.member,
        f"{base}/acceptance-confirmations",
        {"selectionId": selected.json()["selection"]["id"]},
        etag=selected.headers["etag"],
    )


def _accept(
    journey: Journey, subject: Subject, acceptance: httpx.Response, *, outcome: str = "APPROVED"
) -> httpx.Response:
    return _write(
        journey.member,
        f"{subject.base}/acceptance-decisions",
        {
            "gateId": acceptance.json()["gate"]["id"],
            "outcome": outcome,
            "reason": "Creator accepts exact evidence",
        },
        etag=acceptance.headers["etag"],
    )


def _formal_review(journey: Journey, subject: Subject) -> dict[str, Any]:
    base, work = subject.base, subject.work
    current = journey.member.get(base)
    _write(
        journey.member, f"{work}:request-formal-mr", {}, status=202, etag=current.headers["etag"]
    )
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process")["processed"] == 1
    projection = journey.member.get(f"{base}/delivery").json()
    review = next(
        item for item in projection["workItems"] if item["workItem"]["id"] == subject.work_item_id
    )["currentFormalReview"]
    assert review["assignment"]["currentReviewerId"] == journey.leader_id
    return cast(dict[str, Any], review)


def _finish(
    journey: Journey, subject: Subject, review: dict[str, Any], *, expected_state: str = "COMPLETED"
) -> None:
    base, work = subject.base, subject.work
    current = journey.member.get(base)
    reviewed = _write(
        journey.leader,
        f"{base}/formal-review-decisions",
        {
            "gateId": review["gate"]["id"],
            "outcome": "APPROVED",
            "reason": "Direct Leader approves exact formal head",
        },
        etag=current.headers["etag"],
    )
    _write(
        journey.member,
        f"{work}:request-formal-merge",
        {},
        status=202,
        etag=reviewed.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process")["processed"] == 1
    final = journey.member.get(base)
    assert final.json()["requirement"]["state"] == expected_state
    assert (
        next(item for item in final.json()["workItems"] if item["id"] == subject.work_item_id)[
            "formalDeliveryState"
        ]
        == "MERGED"
    )
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT merge_commit_sha FROM source_control.merge_request_observation o "
                    "JOIN source_control.merge_request_binding b ON b.id=o.binding_id "
                    "WHERE b.work_item_id=:id AND b.kind='FORMAL' AND o.state='MERGED'"
                ),
                {"id": subject.work_item_id},
            ).scalar_one()
            == FORMAL_SHA
        )


def test_default_app_and_workers_complete_real_member_delivery(journey: Journey) -> None:
    subject = _ready_subject(journey)
    _integrate(journey, subject)
    acceptance = _open_acceptance(journey, subject)
    snapshot = journey.member.get(f"{subject.base}/delivery").json()["currentDeliverySnapshot"]
    evidence_url = f"{subject.base}/delivery-snapshots/{snapshot['id']}/integration-baseline"
    with TestClient(bootstrap.create_app()) as anonymous:
        assert anonymous.get(evidence_url).status_code == 401
    assert journey.admin.get(evidence_url).status_code == 403
    _accept(journey, subject, acceptance)
    review = _formal_review(journey, subject)
    _finish(journey, subject, review)
    other = _write(
        journey.member,
        "/api/v1/requirements",
        {
            "workspaceId": journey.workspace_id,
            "type": "feat",
            "title": "Snapshot isolation",
            "description": "Cannot discover another Requirement's evidence",
            "acceptanceCriteria": ["Snapshot ownership is checked"],
            "initialRepositoryId": journey.repository_id,
        },
        status=201,
    )
    other_id = other.json()["requirement"]["id"]
    cross_subject = journey.member.get(
        f"/api/v1/requirements/{other_id}/delivery-snapshots/{snapshot['id']}/integration-baseline"
    )
    assert cross_subject.status_code == 404, cross_subject.text


def test_public_two_round_rework_keeps_formal_binding_and_refreshes_review(
    journey: Journey,
) -> None:
    subject = _ready_subject(journey)
    _integrate(journey, subject)
    first = _open_acceptance(journey, subject)
    _accept(journey, subject, first)
    review = _formal_review(journey, subject)
    negative = _write(
        journey.leader,
        f"{subject.base}/formal-review-decisions",
        {
            "gateId": review["gate"]["id"],
            "outcome": "CHANGES_REQUESTED",
            "reason": "First formal round needs rework",
        },
        etag=journey.member.get(subject.base).headers["etag"],
    )
    assert negative.json()["requirement"]["state"] == "IN_PROGRESS"
    original_binding = journey.member.get(subject.base).json()["workItems"][0][
        "formalMergeRequestBindingId"
    ]
    _integrate(journey, subject, head="e" * 40)
    second = _open_acceptance(journey, subject, head="e" * 40, merge_sha="f" * 40)
    rejected = _accept(journey, subject, second, outcome="REJECTED")
    assert rejected.json()["requirement"]["state"] == "IN_PROGRESS"
    _integrate(journey, subject, head="1" * 40)
    third = _open_acceptance(journey, subject, head="1" * 40, merge_sha="2" * 40)
    _accept(journey, subject, third)
    final_review = _formal_review(journey, subject)
    assert final_review["gate"]["id"] != review["gate"]["id"]
    assert final_review["assignment"]["id"] != review["assignment"]["id"]
    assert (
        journey.member.get(subject.base).json()["workItems"][0]["formalMergeRequestBindingId"]
        == original_binding
    )
    _finish(journey, subject, final_review)
    assert len([mr for mr in journey.provider.mrs.values() if mr["target_branch"] == "main"]) == 1
    assert len([mr for mr in journey.provider.mrs.values() if mr["target_branch"] == "dev"]) == 3


def _revoke(journey: Journey, actor_id: str, capability: str) -> None:
    grants = journey.admin.get("/api/v1/admin/grants")
    assert grants.status_code == 200
    grant = next(
        item
        for item in grants.json()["items"]
        if item["principalId"] == actor_id
        and item["capability"] == capability
        and item["scopeId"] == journey.workspace_id
        and item["status"] == "ACTIVE"
    )
    result = _write(
        journey.admin,
        f"/api/v1/admin/grants/{grant['id']}",
        {"reason": "Revoke queued authority"},
        method="DELETE",
        etag=f'"v{grant["version"]}"',
    )
    assert result.json()["status"] == "REVOKED"


def test_revoked_queued_formal_actor_cannot_create_provider_mr(journey: Journey) -> None:
    subject = _ready_subject(journey)
    _integrate(journey, subject)
    acceptance = _open_acceptance(journey, subject)
    accepted = _accept(journey, subject, acceptance)
    _write(
        journey.member,
        f"{subject.work}:request-formal-mr",
        {},
        status=202,
        etag=accepted.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    _revoke(journey, journey.member_id, "formal_merge_request.request")
    writes_before = list(journey.provider.writes)
    journey.worker("process", errors=("OWNER_INELIGIBLE",))
    assert journey.provider.writes == writes_before
    current = journey.member.get(subject.base).json()
    assert current["workItems"][0]["formalDeliveryState"] == "BLOCKED"
    assert current["workItems"][0]["formalBlockedReasonCode"] == "OWNER_INELIGIBLE"
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM source_control.merge_request_binding WHERE kind='FORMAL'"
                )
            ).scalar_one()
            == 0
        )
        assert db.execute(
            text(
                "SELECT state, requirement_callback_state "
                "FROM source_control.source_control_effect "
                "WHERE operation='CREATE_FORMAL_MR'"
            )
        ).one() == ("BLOCKED", "ACKED")
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit.audit_event "
                    "WHERE action='requirement.formal_delivery.blocked' AND result='SUCCESS'"
                )
            ).scalar_one()
            == 1
        )
    with pytest.raises(RuntimeError, match="owner-denial facts exist"):
        command.downgrade(Config("alembic.ini"), "requirement@0008_req_gate_policy")
    assert journey.member.get(subject.base).json() == current


def test_same_head_retry_does_not_fabricate_delivery_commit(journey: Journey) -> None:
    subject = _ready_subject(journey)
    current = journey.member.get(subject.base)
    _write(
        journey.member,
        f"{subject.work}:request-integration-mr",
        {},
        status=202,
        etag=current.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    journey.worker("process", errors=("NO_DELIVERY_COMMIT",))
    blocked = journey.member.get(subject.base)
    assert blocked.json()["workItems"][0]["integrationDeliveryState"] == "IMPLEMENTING"
    assert blocked.json()["workItems"][0]["integrationBlockedReasonCode"] is None
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT last_error_code FROM source_control.delivery_request_inbox")
            ).scalar_one()
            == "NO_DELIVERY_COMMIT"
        )
    before = list(journey.provider.writes)
    _write(
        journey.member,
        f"{subject.work}:request-integration-mr",
        {},
        status=202,
        etag=blocked.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    journey.worker("process", errors=("NO_DELIVERY_COMMIT",))
    assert journey.provider.writes == before
    assert journey.provider.mrs == {}
    _integrate(journey, subject)
    assert len(journey.provider.mrs) == 1


def test_public_assignment_uses_live_grants_and_gate_revision(journey: Journey) -> None:
    subject = _ready_subject(journey)
    _integrate(journey, subject)
    acceptance = _open_acceptance(journey, subject)
    gate = acceptance.json()["gate"]
    path = f"{subject.base}/delivery-gates/{gate['id']}:reassign"
    body = {"candidateId": journey.leader_id, "reason": "Ordinary creator delegation"}
    etag = f'"v{gate["revision"]}"'
    # An equally capable non-default reviewer cannot take assignment ownership.
    _write(journey.leader, path, body, etag=etag, status=403)
    _revoke(journey, journey.member_id, "requirement.delivery_gate.assign")
    _write(journey.member, path, body, etag=etag, status=403)
    _grant(journey.admin, journey.member_id, "requirement.delivery_gate.assign")
    _write(journey.member, path, body, etag=etag, status=403)
    _grant(
        journey.admin, journey.member_id, "requirement.delivery_gate.assign", journey.workspace_id
    )
    _revoke(journey, journey.leader_id, "requirement.acceptance.decide")
    _write(journey.member, path, body, etag=etag, status=403)
    _grant(journey.admin, journey.leader_id, "requirement.acceptance.decide", journey.workspace_id)
    reassigned = _write(journey.member, path, body, etag=etag, key="assignment-exact-replay")
    replay = _write(journey.member, path, body, etag=etag, key="assignment-exact-replay")
    assert replay.json() == reassigned.json()
    assert replay.headers["etag"] == reassigned.headers["etag"]
    _write(journey.member, path, body, etag=etag, status=409)
    decision = {
        "gateId": gate["id"],
        "outcome": "APPROVED",
        "reason": "Only current assignment can decide",
    }
    current = journey.member.get(subject.base)
    _write(
        journey.member,
        f"{subject.base}/acceptance-decisions",
        decision,
        etag=current.headers["etag"],
        status=403,
    )
    _write(
        journey.leader,
        f"{subject.base}/acceptance-decisions",
        decision,
        etag=current.headers["etag"],
    )


def test_unknown_formal_create_reconciles_without_repeating_external_write(
    journey: Journey, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = _ready_subject(journey)
    _integrate(journey, subject)
    acceptance = _open_acceptance(journey, subject)
    accepted = _accept(journey, subject, acceptance)
    journey.provider.timeout_formal_create = True
    request = _write(
        journey.member,
        f"{subject.work}:request-formal-mr",
        {},
        status=202,
        etag=accepted.headers["etag"],
        key="unknown-create-replay",
    )
    replay = _write(
        journey.member,
        f"{subject.work}:request-formal-mr",
        {},
        status=202,
        etag=accepted.headers["etag"],
        key="unknown-create-replay",
    )
    assert replay.json() == request.json()
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process", errors=("EXTERNAL_RESULT_UNKNOWN",))["processed"] == 1
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT state FROM source_control.source_control_effect "
                    "WHERE operation='CREATE_FORMAL_MR'"
                )
            ).scalar_one()
            == "UNKNOWN"
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM source_control.merge_request_binding WHERE kind='FORMAL'"
                )
            ).scalar_one()
            == 0
        )
    writes = list(journey.provider.writes)
    future = datetime.now(UTC) + timedelta(minutes=3)
    monkeypatch.setattr(SystemClock, "now", lambda _self: future)
    assert journey.worker("reconcile")["processed"] == 1
    assert journey.provider.writes == writes
    projection = journey.member.get(f"{subject.base}/delivery").json()
    review = projection["workItems"][0]["currentFormalReview"]
    assert review["assignment"]["currentReviewerId"] == journey.leader_id
    with journey.database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT state, requirement_callback_state "
                "FROM source_control.source_control_effect "
                "WHERE operation='CREATE_FORMAL_MR'"
            )
        ).one() == ("SUCCEEDED", "ACKED")
    # Public scans after ACK never manufacture a second callback, gate, or binding.
    assert journey.worker("relay")["processed"] == 0
    assert journey.worker("process")["processed"] == 0
    assert journey.worker("reconcile")["processed"] == 0
    assert journey.member.get(f"{subject.base}/delivery").json() == projection
    _finish(journey, subject, review)


def test_two_required_work_items_complete_only_after_both_formal_merges(journey: Journey) -> None:
    subject = _ready_subject(journey, count=2)
    peer_id = subject.peers[0]
    peer = replace(
        subject, work_item_id=peer_id, work=f"{subject.base}/work-items/{peer_id}", peers=()
    )
    _open_integration_mr(journey, subject)
    partial = journey.member.get(subject.base)
    assert partial.json()["requirement"]["state"] == "IN_PROGRESS"
    _write(
        journey.member,
        f"{subject.base}:request-integration-baseline",
        {"expectedRequirementVersion": partial.json()["requirement"]["requirementVersion"]},
        status=409,
        etag=partial.headers["etag"],
    )
    _open_integration_mr(journey, peer)
    _merge_integration(journey, subject)
    _merge_integration(journey, peer)
    acceptance = _open_acceptance(journey, subject)
    _accept(journey, subject, acceptance)
    first_review = _formal_review(journey, subject)
    second_review = _formal_review(journey, peer)
    _finish(journey, subject, first_review, expected_state="AWAITING_MERGE")
    _finish(journey, peer, second_review)
    listing = journey.member.get(
        "/api/v1/requirements", params={"workspaceId": journey.workspace_id}
    )
    assert listing.status_code == 200
    assert listing.json()["items"][0]["state"] == "COMPLETED"
    delivery = journey.member.get(f"{subject.base}/delivery").json()
    assert all(item["workItem"]["state"] == "COMPLETED" for item in delivery["workItems"])


def test_public_policy_publish_preserves_frozen_gate_and_changes_next_archive(
    journey: Journey, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = _ready_subject(journey)
    _integrate(journey, subject)
    acceptance = _open_acceptance(journey, subject)
    assert acceptance.json()["gate"]["policyVersion"] == 1
    base = "/api/v1/admin/policies/requirement.gate/drafts"
    stale = _write(journey.admin, base, {"values": {}}, status=201)
    draft = _write(
        journey.admin,
        base,
        {
            "values": {
                "acceptance.additional_required_capabilities": ["code.change"],
                "draft_archive_after_days": 7,
            }
        },
        status=201,
    )
    draft_path = f"{base}/{draft.json()['id']}"
    validated = _write(journey.admin, f"{draft_path}/validate", {}, etag=draft.headers["etag"])
    assert validated.json()["valid"] is True
    preview = journey.admin.get(
        f"{draft_path}/preview", headers={"If-Match": validated.headers["etag"]}
    )
    assert preview.status_code == 200, preview.text
    archive_at = datetime.fromisoformat(draft.json()["lastMeaningfulActivityAt"]) + timedelta(
        days=8
    )
    runtime = bootstrap.requirement_policy_runtime()
    # Draft-only edits cannot move the NEXT_SCHEDULE interval from the active 30 days.
    assert runtime.archive(now=archive_at) == 0
    assert runtime.resolved_snapshot().policy.draft_archive_after_days == 30
    now = datetime.now(UTC) + timedelta(seconds=30)
    monkeypatch.setattr(SystemClock, "now", lambda _self: now)
    published = _write(
        journey.admin,
        f"{draft_path}/publish",
        {"reason": "Governed stronger policy", "totpCode": pyotp.TOTP(journey.admin_totp).at(now)},
        status=201,
        etag=preview.headers["etag"],
    )
    assert published.json()["version"] == 2
    assert runtime.resolved_snapshot().policy.draft_archive_after_days == 7
    stale_publish = _write(
        journey.admin,
        f"{base}/{stale.json()['id']}/publish",
        {
            "reason": "Must reject stale base before reauthentication",
            "totpCode": pyotp.TOTP(journey.admin_totp).at(now),
        },
        status=409,
        etag=stale.headers["etag"],
    )
    assert stale_publish.json()["code"] == "SOURCE_STALE"
    assert runtime.archive(now=archive_at) == 2
    current = journey.member.get(f"{subject.base}/delivery").json()
    assert current["currentAcceptance"]["gate"] == acceptance.json()["gate"]
    _accept(journey, subject, acceptance)
    with journey.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 1
        )
        assert (
            db.execute(text("SELECT count(*) FROM requirement.gate_policy_version")).scalar_one()
            == 2
        )


def test_formal_owner_denial_migration_round_trip_preserves_closed_reason_set(
    production_database: ProductionDatabase,
) -> None:
    def assert_constraint(*, owner_allowed: bool) -> None:
        with production_database.owner.begin() as db:
            check = db.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='requirement.work_item'::regclass "
                    "AND conname='ck_req_work_item_formal_block'"
                )
            ).scalar_one()
            # Execute the installed CHECK in a temporary constraint probe, not owner state.
            db.execute(
                text(
                    "CREATE TEMP TABLE formal_reason_probe (formal_delivery_state text, "
                    f"formal_blocked_reason_code text, {check}) ON COMMIT DROP"
                )
            )
            for reason in (
                "MERGE_ACTOR_INELIGIBLE",
                "REPOSITORY_NOT_AUTHORIZED",
                "BRANCH_BINDING_MISSING",
                "TARGET_BRANCH_NOT_FOUND",
                "TARGET_BRANCH_NOT_PROTECTED",
                "NO_DELIVERY_COMMIT",
                "HEAD_SHA_CHANGED",
                "MR_CONFLICT",
                "MR_CLOSED",
                "MR_CHECKS_BLOCKED",
                "MERGE_CONFLICT",
                "PROJECT_PROFILE_UNSUPPORTED",
                "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION",
                "EXTERNAL_MERGE_DRIFT",
            ):
                db.execute(
                    text("INSERT INTO formal_reason_probe VALUES ('BLOCKED', :reason)"),
                    {"reason": reason},
                )
            if owner_allowed:
                db.execute(
                    text("INSERT INTO formal_reason_probe VALUES ('BLOCKED', 'OWNER_INELIGIBLE')")
                )
            else:
                with pytest.raises(IntegrityError), db.begin_nested():
                    db.execute(
                        text(
                            "INSERT INTO formal_reason_probe VALUES ('BLOCKED', 'OWNER_INELIGIBLE')"
                        )
                    )
            for state, reason in (("BLOCKED", "UNKNOWN_REASON"), ("MR_OPEN", "OWNER_INELIGIBLE")):
                with pytest.raises(IntegrityError), db.begin_nested():
                    db.execute(
                        text("INSERT INTO formal_reason_probe VALUES (:state, :reason)"),
                        {"state": state, "reason": reason},
                    )

    assert_constraint(owner_allowed=True)
    command.downgrade(Config("alembic.ini"), "requirement@0008_req_gate_policy")
    assert_constraint(owner_allowed=False)
    command.upgrade(Config("alembic.ini"), "heads")
    assert_constraint(owner_allowed=True)


def test_observed_formal_success_replay_survives_later_request_grant_revocation(
    journey: Journey,
) -> None:
    subject = _ready_subject(journey)
    _integrate(journey, subject)
    acceptance = _open_acceptance(journey, subject)
    _accept(journey, subject, acceptance)
    _formal_review(journey, subject)
    with journey.database.owner.connect() as db:
        message_id = str(
            db.execute(
                text(
                    "SELECT message_id FROM source_control.formal_delivery_request_inbox "
                    "WHERE topic='requirement.formal-merge-request.requested'"
                )
            ).scalar_one()
        )
    _revoke(journey, journey.member_id, "formal_merge_request.request")
    before = journey.member.get(f"{subject.base}/delivery").json()
    writes = list(journey.provider.writes)
    runtime = build_source_control_runtime(
        journey.settings,
        collaborators=default_source_control_collaborators(),
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(journey.provider), **kwargs
        ),
    )
    try:
        for _ in range(2):
            replay = process_formal_delivery_request(
                message_id=message_id, dependencies=runtime.dependencies
            )
            assert replay.effect is not None
            assert replay.effect.state.value == "SUCCEEDED"
            assert replay.effect.callback_state.value == "ACKED"
    finally:
        runtime.close()
    assert journey.provider.writes == writes
    assert journey.member.get(f"{subject.base}/delivery").json() == before
