from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

from sqlalchemy import Engine, text

import control_plane.app.modules.identity as identity
from control_plane.app.modules.authorization import (
    AuthorizationDependencies,
    InitialProvisioningDenied,
    provision_initial_admin_grants,
)
from control_plane.app.modules.identity import IdentityDependencies


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap the environment's first Super Admin account.",
    )
    parser.add_argument("--employee-no", required=True)
    parser.add_argument("--display-name", required=True)
    return parser


def _runtime() -> tuple[
    Engine,
    IdentityDependencies,
    Any,
    Engine,
    AuthorizationDependencies,
]:
    from control_plane.app.bootstrap.app import (
        authorization_dependencies,
        authorization_runtime_engine,
        identity_dependencies,
        identity_runtime_engine,
        security_change_orchestrator,
    )

    return (
        identity_runtime_engine(),
        identity_dependencies(),
        security_change_orchestrator(),
        authorization_runtime_engine(),
        authorization_dependencies(),
    )


def _write_evidence(evidence: TextIO, payload: dict[str, str]) -> None:
    evidence.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_evidence_safely(evidence: TextIO, payload: dict[str, str]) -> None:
    try:
        _write_evidence(evidence, payload)
    except Exception:
        # Evidence delivery cannot make a possibly committed credential change
        # look unexecuted. The database Audit remains the authoritative fact.
        pass


def _resolve_committed(
    engine: Engine,
    *,
    args: argparse.Namespace,
    dependencies: IdentityDependencies,
) -> identity.SuperAdminCliExecution | None:
    with engine.begin() as db:
        return identity.resolve_bootstrap_cli(
            db,
            employee_no=args.employee_no,
            display_name=args.display_name,
            dependencies=dependencies,
        )


def _transaction_status(engine: Engine, source_transaction_id: str) -> str:
    try:
        with engine.connect() as db:
            return str(
                db.execute(
                    text("SELECT pg_xact_status(CAST(:source_xid AS xid8))"),
                    {"source_xid": source_transaction_id},
                ).scalar_one()
            )
    except Exception:
        return "unknown"


def main(
    argv: list[str] | None = None,
    *,
    engine: Engine | None = None,
    dependencies: IdentityDependencies | None = None,
    security_changes: Any = None,
    authorization_engine: Engine | None = None,
    authorization_dependencies: AuthorizationDependencies | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    output = stdout or sys.stdout
    evidence = stderr or sys.stderr
    if engine is None or dependencies is None:
        (
            runtime_engine,
            runtime_dependencies,
            runtime_security_changes,
            runtime_authorization_engine,
            runtime_authorization_dependencies,
        ) = _runtime()
        engine = engine or runtime_engine
        dependencies = dependencies or runtime_dependencies
        if security_changes is None:
            security_changes = runtime_security_changes
        authorization_engine = authorization_engine or runtime_authorization_engine
        authorization_dependencies = (
            authorization_dependencies or runtime_authorization_dependencies
        )
    elif authorization_engine is None or authorization_dependencies is None:
        # Tests and embedded callers may intentionally exercise the Identity-only
        # primitive. The real CLI runtime always supplies both authorization values.
        authorization_engine = None
        authorization_dependencies = None

    execution = None
    commit_attempted = False
    try:
        with engine.connect() as db:
            transaction = db.begin()
            source_transaction_id = ""
            commit_error: Exception | None = None
            try:
                source_transaction_id = str(
                    db.execute(text("SELECT pg_current_xact_id()")).scalar_one()
                )
                execution = identity.bootstrap_super_admin_cli(
                    db,
                    employee_no=args.employee_no,
                    display_name=args.display_name,
                    source_transaction_id=source_transaction_id,
                    dependencies=dependencies,
                )
                output.write(f"{execution.temporary_password}\n")
                output.flush()
                commit_attempted = True
                try:
                    transaction.commit()
                except Exception as error:
                    commit_error = error
            except Exception:
                if transaction.is_active:
                    transaction.rollback()
                raise
        assert execution is not None
        if commit_error is not None:
            try:
                resolved = _resolve_committed(
                    engine,
                    args=args,
                    dependencies=dependencies,
                )
            except Exception:
                resolved = None
            if resolved is None:
                status = _transaction_status(engine, source_transaction_id)
                if status == "aborted":
                    _write_evidence_safely(
                        evidence,
                        {"event": "super_admin_bootstrap", "result": "FAILED"},
                    )
                    return 4
                _write_evidence_safely(
                    evidence,
                    {
                        "event": "super_admin_bootstrap",
                        "result": "OUTCOME_UNKNOWN",
                        "commandId": execution.correlation_id,
                    },
                )
                return 0
            if resolved.temporary_password != execution.temporary_password:
                _write_evidence_safely(
                    evidence,
                    {
                        "event": "super_admin_bootstrap",
                        "result": "OUTCOME_UNKNOWN",
                        "commandId": execution.correlation_id,
                    },
                )
                return 0
    except (ValueError, identity.SuperAdminBootstrapConflict):
        if commit_attempted:
            _write_evidence_safely(
                evidence,
                {
                    "event": "super_admin_bootstrap",
                    "result": "OUTCOME_UNKNOWN",
                    **({"commandId": execution.correlation_id} if execution is not None else {}),
                },
            )
            return 0
        _write_evidence_safely(
            evidence,
            {"event": "super_admin_bootstrap", "result": "DENIED"},
        )
        return 3
    except Exception:
        if commit_attempted:
            _write_evidence_safely(
                evidence,
                {
                    "event": "super_admin_bootstrap",
                    "result": "OUTCOME_UNKNOWN",
                    **({"commandId": execution.correlation_id} if execution is not None else {}),
                },
            )
            return 0
        _write_evidence_safely(
            evidence,
            {"event": "super_admin_bootstrap", "result": "FAILED"},
        )
        return 4

    if security_changes is not None and execution is not None:
        try:
            for ticket in execution.tickets:
                security_changes.complete(ticket)
            security_changes.reconcile_pending()
        except Exception:
            # The committed bootstrap is protected by the persistent fail-closed
            # fence; a non-zero exit would incorrectly invite a second bootstrap.
            pass
    if (
        execution is not None
        and authorization_engine is not None
        and authorization_dependencies is not None
    ):
        provisioning_denial: InitialProvisioningDenied | None = None
        try:
            with authorization_engine.begin() as db:
                try:
                    provision_initial_admin_grants(
                        db,
                        principal_id=execution.account.id,
                        command_id=execution.correlation_id,
                        dependencies=authorization_dependencies,
                    )
                except InitialProvisioningDenied as error:
                    # The denial and its idempotency outcome are security evidence.
                    # Commit them before surfacing the rejected command.
                    provisioning_denial = error
        except Exception:
            _write_evidence_safely(
                evidence,
                {
                    "event": "super_admin_bootstrap",
                    "result": "FAILED",
                    "employeeNo": args.employee_no,
                    "commandId": execution.correlation_id,
                },
            )
            return 4
        if provisioning_denial is not None:
            _write_evidence_safely(
                evidence,
                {
                    "event": "super_admin_bootstrap",
                    "result": "DENIED",
                    "employeeNo": args.employee_no,
                    "commandId": execution.correlation_id,
                },
            )
            return 3
    _write_evidence_safely(
        evidence,
        {
            "event": "super_admin_bootstrap",
            "result": "SUCCESS",
            "employeeNo": args.employee_no,
            "commandId": execution.correlation_id,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
